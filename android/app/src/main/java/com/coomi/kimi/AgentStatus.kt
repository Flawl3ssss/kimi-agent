package com.coomi.kimi

/**
 * The runtime's self-reported state, read by the activity once a second.
 *
 * A plain object rather than a bound service: the only writer is AgentService's
 * worker thread and the only reader is this app's UI thread, and the values are
 * three tiny strings. `onBind` returns null, so there is no IPC to keep correct.
 */
object AgentStatus {
    const val PHASE_IDLE = "idle"
    const val PHASE_UNPACK = "unpack"
    const val PHASE_STARTING = "starting"
    const val PHASE_RUNNING = "running"
    const val PHASE_EXITED = "exited"
    const val PHASE_FAILED = "failed"

    @Volatile var phase: String = PHASE_IDLE
    @Volatile var message: String = "Ожидание…"
    @Volatile var lastLog: String = ""
}
