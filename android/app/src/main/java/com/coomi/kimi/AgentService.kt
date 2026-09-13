package com.coomi.kimi

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import java.io.File
import java.io.FileWriter
import java.io.PrintWriter

/**
 * Owns the agent process: unpacks the runtime on first start, then keeps
 * `python -m kimi_agent.cli serve` alive under proot and reports status.
 *
 * The child runs as this app's own UID — the same model Termux uses, which is
 * what lets a full Linux userland work without root.
 */
class AgentService : Service() {

    private var proc: Process? = null
    private var bootstrap: Bootstrap? = null
    private val main = Handler(Looper.getMainLooper())
    private var logWriter: PrintWriter? = null
    @Volatile private var stopping = false
    @Volatile private var restarting = false
    @Volatile private var consecutiveExits = 0
    @Volatile private var ensuring = false

    /** Required by Service; the UI reads [AgentStatus] instead of talking to us. */
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        channel()
        startForegroundCompat()
        logFile().parentFile?.mkdirs()
        logWriter = runCatching { PrintWriter(FileWriter(logFile(), true), true) }.getOrNull()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopping = true
                proc?.destroy()
                proc = null
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_RESTART -> {
                main.post { restartAgent("requested") }
                return START_STICKY
            }
        }
        main.post { ensure() }
        return START_STICKY
    }

    /**
     * Unpacking runs off the main thread on purpose: the rootfs is ~250 MB
     * expanded, and doing it inside `onStartCommand` would block the thread that
     * also draws the activity and answers the foreground-service contract.
     *
     * [ensuring] is the guard: a redelivered START_STICKY must not start a second
     * unpack into the same directories while the first one is still writing.
     */
    private fun ensure() {
        if (ensuring) return
        ensuring = true
        Thread {
            try {
                val bs = bootstrap ?: Bootstrap(this) { log(it) }.also { bootstrap = it }
                AgentStatus.phase = AgentStatus.PHASE_UNPACK
                AgentStatus.message = "Проверка runtime…"
                if (!bs.prepare { msg -> AgentStatus.message = msg; log(msg) }) {
                    AgentStatus.phase = AgentStatus.PHASE_FAILED
                    AgentStatus.message = "Runtime не распакован — смотрите лог"
                    return@Thread
                }
                AgentStatus.phase = AgentStatus.PHASE_STARTING
                AgentStatus.message = "Запуск агента…"
                spawn()
            } finally {
                ensuring = false
            }
        }.start()
    }

    /**
     * The command line comes from [RuntimeSpec], which the JVM unit tests cover.
     * Building it twice — once here, once in the spec — is how these drift apart
     * and the tests start proving nothing.
     */
    private fun spawn() {
        val bs = bootstrap ?: return
        val layout = bs.layout()
        val argv = RuntimeSpec.buildCommand(
            layout,
            mapOf(
                "COOMI_ANDROID" to "1",
                "COOMI_VERSION_NAME" to BuildConfig.VERSION_NAME,
            ),
        )
        log("spawn: ${argv.joinToString(" ")}")
        val p = runCatching {
            ProcessBuilder(argv).redirectErrorStream(true).apply {
                // proot reads its own settings from the Android-side environment;
                // the guest's `env -i` cannot carry them because proot parses
                // these before it ever execs the guest program.
                environment().putAll(RuntimeSpec.prootEnv(layout))
            }.start()
        }.getOrElse {
            AgentStatus.phase = AgentStatus.PHASE_FAILED
            AgentStatus.message = "proot не запустился: ${it.message}"
            log("spawn failed: ${it}")
            return
        }
        proc = p
        watchStartup(p)
        Thread {
            p.inputStream.bufferedReader().use { r ->
                r.lineSequence().forEach { line -> log("agent: $line") }
            }
            val code = runCatching { p.waitFor() }.getOrDefault(-1)
            if (stopping || restarting) return@Thread
            // Mutating the retry state on the main thread is what keeps this
            // counter and spawn() from racing with restartAgent().
            main.post {
                proc = null
                consecutiveExits++
                // A broken proot fails within milliseconds; a fixed 3-second retry
                // would become a hot loop that eats the battery and the log file.
                val delay = minOf(60_000L, 2_000L * (1L shl minOf(5, consecutiveExits - 1)))
                AgentStatus.phase = AgentStatus.PHASE_EXITED
                AgentStatus.message =
                    "Код $code, попыток $consecutiveExits — рестарт через ${delay / 1000} с"
                log("agent exited code=$code, retry in ${delay}ms")
                if (!stopping && !restarting) main.postDelayed({ spawn() }, delay)
            }
        }.start()
    }

    /**
     * "Running" means the port accepts a connection. Matching a log line for a
     * URL would break the moment the agent rewords its startup message, and the
     * UI would show a green light over a dead socket.
     */
    private fun watchStartup(p: Process) {
        Thread {
            val deadline = System.currentTimeMillis() + 180_000
            while (System.currentTimeMillis() < deadline && p.isAlive) {
                if (portOpen()) {
                    consecutiveExits = 0
                    AgentStatus.phase = AgentStatus.PHASE_RUNNING
                    AgentStatus.message = "Агент на 127.0.0.1:${RuntimeSpec.PORT}"
                    return@Thread
                }
                Thread.sleep(500)
            }
            if (p.isAlive) {
                AgentStatus.message = "Порт ${RuntimeSpec.PORT} не слушается — смотрите лог"
            }
        }.start()
    }

    private fun portOpen(): Boolean = runCatching {
        java.net.Socket().use { it.connect(java.net.InetSocketAddress("127.0.0.1", RuntimeSpec.PORT), 300) }
        true
    }.getOrDefault(false)

    /** Used after config.toml is rewritten: the kernel only reads it at startup. */
    fun restartAgent(reason: String) {
        log("restart requested: $reason")
        restarting = true
        val p = proc
        proc = null
        p?.destroy()
        AgentStatus.phase = AgentStatus.PHASE_STARTING
        AgentStatus.message = "Перезапуск ($reason)"
        main.postDelayed({
            restarting = false
            if (!stopping) spawn()
        }, 800)
    }

    override fun onDestroy() {
        stopping = true
        proc?.destroy()
        proc = null
        runCatching { logWriter?.flush(); logWriter?.close() }
        super.onDestroy()
    }

    // ---------------------------------------------------------------- util

    private fun logFile() = File(filesDir, "logs/agent-service.log")

    private fun log(msg: String) {
        AgentStatus.lastLog = msg
        val line = "${System.currentTimeMillis()} $msg"
        logWriter?.println(line)
        android.util.Log.i("coomi-kimi", line)
    }

    private fun channel() {
        if (android.os.Build.VERSION.SDK_INT >= 26) {
            val nm = getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL, getString(R.string.channel_name), NotificationManager.IMPORTANCE_LOW)
                    .apply { description = getString(R.string.channel_desc) }
            )
        }
    }

    private fun startForegroundCompat() {
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        @Suppress("DEPRECATION")
        val builder = if (android.os.Build.VERSION.SDK_INT >= 26)
            Notification.Builder(this, CHANNEL) else Notification.Builder(this)
        val n = builder
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(AgentStatus.message)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentIntent(pi)
            .setOngoing(true)
            .build()
        // Android 14 wants a foreground-service type; specialUse is the honest one
        // here (a local dev runtime), and the manifest declares it.
        if (android.os.Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, n, android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    companion object {
        const val CHANNEL = "agent"
        const val NOTIF_ID = 42
        const val ACTION_STOP = "com.coomi.kimi.STOP"
        const val ACTION_RESTART = "com.coomi.kimi.RESTART"

        fun start(ctx: Context) {
            val i = Intent(ctx, AgentService::class.java)
            if (android.os.Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i)
        }

        fun restart(ctx: Context) {
            ctx.startService(Intent(ctx, AgentService::class.java).setAction(ACTION_RESTART))
        }
    }
}
