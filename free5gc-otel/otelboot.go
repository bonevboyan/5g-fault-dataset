// OTel SDK bootstrap for free5GC NFs (dataset instrumentation).
// Dropped into each NF's cmd/ package; runs via init() before main().
// free5GC already wraps every SBI call (client via openapi NewHttp2Client, server via
// http2_util) with otelhttp, but ships no SDK/exporter/propagator, so tracing is dormant.
// This installs a global TracerProvider (OTLP/gRPC exporter) and the W3C tracecontext
// propagator, which activates real distributed traces with parent/child spans across NFs.
// No-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set, so stock behaviour is unchanged.
package main

import (
	"context"
	"os"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.24.0"
)

func init() {
	ep := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
	if ep == "" {
		return // tracing disabled -> stock free5GC behaviour
	}
	// Accept "host:port" or "http://host:port"; gRPC exporter wants host:port.
	ep = strings.TrimPrefix(strings.TrimPrefix(ep, "http://"), "https://")
	ep = strings.TrimSuffix(ep, "/")

	ctx := context.Background()
	exp, err := otlptracegrpc.New(ctx,
		otlptracegrpc.WithEndpoint(ep),
		otlptracegrpc.WithInsecure(),
	)
	if err != nil {
		return
	}

	svc := os.Getenv("OTEL_SERVICE_NAME")
	if svc == "" {
		svc = "free5gc"
	}
	res, _ := resource.New(ctx, resource.WithAttributes(semconv.ServiceName(svc)))

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exp, sdktrace.WithBatchTimeout(2*time.Second)),
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sdktrace.AlwaysSample()),
	)
	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))
}
