package openapi

// [f5g patch] Changes to free5gc/openapi v1.3.0 for the 5G fault dataset testbed:
//
//  1. SBI TLS client identity. Upstream never presents a client certificate, so an NRF with
//     OAuth + https (which requires and verifies client certs) rejects every NF. The client now
//     presents SBI_TLS_CERT/SBI_TLS_KEY when the server asks for one.
//  2. Server verification. Upstream sets InsecureSkipVerify. If SBI_TLS_CA is set, servers are
//     verified against that CA (hostname = the short service name used in the SBI URI).
//  3. Tracing. The httptrace sub-spans are created inside the otelhttp client span.
//  4. End-to-end traces. An outgoing SBI request whose context carries no span is parented on
//     the goroutine's current parent (the server span of the request being handled, or the AMF's
//     NGAP / SMF's PFCP root span); see github.com/free5gc/f5gtrace.

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net/http"
	"net/http/httptrace"
	"os"
	"sync"

	"go.opentelemetry.io/contrib/instrumentation/net/http/httptrace/otelhttptrace"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	"github.com/free5gc/f5gtrace"
)

// goroutineParentTransport parents context-less requests on the goroutine's current span.
type goroutineParentTransport struct{ next http.RoundTripper }

func (t goroutineParentTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if ctx := f5gtrace.WithParent(r.Context()); ctx != r.Context() {
		r = r.WithContext(ctx)
	}
	return t.next.RoundTrip(r)
}

func withGoroutineParent(next http.RoundTripper) http.RoundTripper {
	return goroutineParentTransport{next: next}
}

var clientTrace = otelhttp.WithClientTrace(func(ctx context.Context) *httptrace.ClientTrace {
	return otelhttptrace.NewClientTrace(ctx)
})

var (
	clientCertOnce sync.Once
	clientCert     *tls.Certificate
	clientCertErr  error
)

func loadClientCert() (*tls.Certificate, error) {
	clientCertOnce.Do(func() {
		certFile, keyFile := os.Getenv("SBI_TLS_CERT"), os.Getenv("SBI_TLS_KEY")
		if certFile == "" || keyFile == "" {
			clientCert = &tls.Certificate{} // no identity configured: send an empty certificate
			return
		}
		c, err := tls.LoadX509KeyPair(certFile, keyFile)
		if err != nil {
			clientCertErr = fmt.Errorf("load SBI client cert: %w", err)
			return
		}
		clientCert = &c
	})
	return clientCert, clientCertErr
}

func sbiClientTLSConfig() *tls.Config {
	cfg := &tls.Config{
		GetClientCertificate: func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
			return loadClientCert()
		},
	}
	if caFile := os.Getenv("SBI_TLS_CA"); caFile != "" {
		pem, err := os.ReadFile(caFile)
		pool := x509.NewCertPool()
		if err == nil && pool.AppendCertsFromPEM(pem) {
			cfg.RootCAs = pool
			return cfg
		}
		fmt.Fprintf(os.Stderr, "[f5g patch] SBI_TLS_CA %q unusable (%v); not verifying servers\n", caFile, err)
	}
	cfg.InsecureSkipVerify = true // #nosec G402 — upstream behaviour when no CA is configured
	return cfg
}
