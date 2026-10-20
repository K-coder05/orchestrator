# macOS is the only supported host

Nightshift runs only on the MacBook that sits open overnight, using `launchd`, `caffeinate`, `pmset` and the Keychain directly. A Windows port (Task Scheduler, `SetThreadExecutionState`, Credential Manager) was considered and rejected so Phase 1 can ship by Nov 2. Development happens on the Mac itself, so code is edited where it runs.

## Consequences

- OS-specific calls sit behind one thin host seam with a fake for tests, so the core logic (scheduler, priority, catch-up, dedupe, transitions, baselines) is testable on any OS. Only the gate requires the Mac.
- Supporting another OS later means writing a second host implementation, not touching core code.
