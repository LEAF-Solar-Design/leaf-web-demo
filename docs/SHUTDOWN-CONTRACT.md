# Shutdown contract

Long-lived Leaf processes must stop cleanly. A force kill is cleanup, not a
passing result.

The backend process harness sends terminate and waits 10 seconds. If a child
does not exit, the harness kills it, waits for cleanup, writes a bounded JSON
failure receipt beside the existing child log, and fails the test. The receipt
records elapsed time, return code, log path, log size, and any cleanup error.
Module cleanup always attempts both app and broker processes before reporting
failures.

The author harness stops accepting new connections on `SIGINT` or `SIGTERM`.
It allows up to 25 seconds for active connections to close, which stays inside
the ECS 30-second container stop timeout. A graceful stop records elapsed time,
stops the git worker, and exits zero. The fallback first marks shutdown as
failed, closes all connections, stops the git worker, records elapsed time, and
exits one. Repeated signals and close callbacks cannot overwrite that result.

The production preparation budget remains the 300-second target deregistration
delay, plus three 30-second dependent-container stop windows, plus a measured
ECS reporting buffer. This source change does not alter that infrastructure
budget or deploy any service.
