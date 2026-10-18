// Package f5gtrace carries a trace parent across free5GC's call graph.
//
// free5GC instruments every SBI client and server call with otelhttp, but it never threads the
// incoming request's context.Context into the outgoing calls it makes while handling it: the
// consumer layer builds a fresh context (GetTokenCtx / context.TODO) for each call. The server span
// therefore has no children, and each hop starts a new trace.
//
// Threading ctx through every NF's processor and consumer layer is a large mechanical change
// (hundreds of signatures). Instead this package keeps a goroutine-local "current parent":
//
//   - the shared gin constructor (free5gc/util) stores the request context for the handler's
//     goroutine (Set); the AMF NGAP dispatcher and the SMF PFCP dispatcher do the same after starting
//     a root span for the message they received;
//   - the shared SBI client transport (free5gc/openapi) uses it as the parent of any outgoing call
//     whose context carries no span (Current);
//   - the few `go` statements on the path to an SBI call use Go to hand the parent to the child
//     goroutine.
//
// Goroutine identity comes from the "goroutine N [" header of runtime.Stack, which is stable across
// Go versions. One call costs a few microseconds, far below an SBI round-trip.
package f5gtrace

import (
	"bytes"
	"context"
	"runtime"
	"strconv"
	"sync"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

var parents sync.Map // goroutine id (uint64) -> context.Context

func goid() uint64 {
	var buf [64]byte
	n := runtime.Stack(buf[:], false)
	b := bytes.TrimPrefix(buf[:n], []byte("goroutine "))
	if i := bytes.IndexByte(b, ' '); i > 0 {
		b = b[:i]
	}
	id, _ := strconv.ParseUint(string(b), 10, 64)
	return id
}

// Set makes ctx the current parent for the calling goroutine until the returned function runs.
// Typical use: defer f5gtrace.Set(ctx)().
func Set(ctx context.Context) (restore func()) {
	id := goid()
	prev, had := parents.Load(id)
	parents.Store(id, ctx)
	return func() {
		if had {
			parents.Store(id, prev)
		} else {
			parents.Delete(id)
		}
	}
}

// Current returns the calling goroutine's parent context, or context.Background().
func Current() context.Context {
	if v, ok := parents.Load(goid()); ok {
		return v.(context.Context)
	}
	return context.Background()
}

// Go runs f in a new goroutine that inherits the caller's current parent.
func Go(f func()) {
	ctx := Current()
	go func() {
		defer Set(ctx)()
		f()
	}()
}

// Start begins a span as a child of the current parent (or a new root), makes it current, and
// returns the function that ends it and restores the previous parent.
func Start(name string, attrs ...attribute.KeyValue) (end func(err error)) {
	ctx, span := otel.Tracer("f5g").Start(Current(), name, trace.WithAttributes(attrs...))
	restore := Set(ctx)
	return func(err error) {
		if err != nil {
			span.RecordError(err)
			span.SetStatus(codes.Error, err.Error())
		}
		span.End()
		restore()
	}
}

// Annotate adds attributes to the current span, if there is one.
func Annotate(attrs ...attribute.KeyValue) {
	trace.SpanFromContext(Current()).SetAttributes(attrs...)
}

// WithParent returns ctx with the current goroutine's span as its parent if ctx has none.
// The span object itself is carried over (not just its SpanContext): otelhttp's transport takes
// its tracer from the parent span's TracerProvider, and a bare SpanContext would give it the
// no-op provider, which silently drops the client span and the httptrace sub-spans.
func WithParent(ctx context.Context) context.Context {
	if trace.SpanContextFromContext(ctx).IsValid() {
		return ctx
	}
	if span := trace.SpanFromContext(Current()); span.SpanContext().IsValid() {
		return trace.ContextWithSpan(ctx, span)
	}
	return ctx
}
