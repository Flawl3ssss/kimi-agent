package com.coomi.kimi

import java.io.File

/**
 * Everything about *how* the agent is launched, as pure data.
 *
 * Kept free of Android types so that it is unit-testable on the JVM (CI runs
 * these without an emulator). The shapes here mirror the proot invocation that
 * Coomi itself uses on this device, which is the known-good reference:
 *
 *   proot --kill-on-exit -0 -r <rootfs> -b <home>:/home/coomi -b <tmp>:/tmp
 *         -b /proc -b /dev -w /workspace
 *         /usr/bin/env -i HOME=... PATH=... LANG=C.UTF-8 <prog> <args...>
 */
object RuntimeSpec {

    /** Guest-side locations. The host-side paths live in [Layout]. */
    const val GUEST_WORKSPACE = "/workspace"
    const val GUEST_AGENT = "/opt/agent"
    const val GUEST_HOME = "/home/coomi"
    const val GUEST_TMP = "/tmp"
    const val GUEST_KIMI = "/usr/local/bin/kimi"

    /** Must match what the rootfs provides; python3 is part of the shipped image. */
    const val GUEST_PYTHON = "/usr/bin/python3"
    const val GUEST_SITE_PACKAGES = "/opt/deps"

    const val PORT = 8765
    const val BRIDGE_PORT = 8766

    /**
     * Environment handed to the agent. `env -i` wipes the inherited one, so
     * anything the process needs has to appear here explicitly.
     */
    fun agentEnv(): List<Pair<String, String>> = listOf(
        "HOME" to GUEST_HOME,
        "PATH" to "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "TMPDIR" to GUEST_TMP,
        "LANG" to "C.UTF-8",
        "LC_ALL" to "C.UTF-8",
        "SSL_CERT_FILE" to "/etc/ssl/certs/ca-certificates.crt",
        // Kimi Code reads config/credentials from here; it is a symlink to the
        // app-private directory, so the login survives an app update.
        "KIMI_CODE_HOME" to "$GUEST_HOME/.kimi-code",
        "COOMI_KIMI_HOME" to "$GUEST_HOME/.coomi-kimi",
        "COOMI_KIMI_WORKSPACE" to GUEST_WORKSPACE,
        "COOMI_KIMI_BIN" to GUEST_KIMI,
        "COOMI_KIMI_HOST" to "127.0.0.1",
        "COOMI_KIMI_PORT" to PORT.toString(),
        "COOMI_KIMI_BRIDGE_PORT" to BRIDGE_PORT.toString(),
        // The agent's Python deps are unpacked as a plain directory and put on
        // PYTHONPATH: a venv would bake absolute interpreter paths and break.
        "PYTHONPATH" to "$GUEST_AGENT:$GUEST_SITE_PACKAGES",
        "PYTHONDONTWRITEBYTECODE" to "1",
        "NO_COLOR" to "1",
        "PYTHONUNBUFFERED" to "1",
        // A phone has no room for a 256 K-token window by default; the model
        // entry in config.toml carries the real number the user configured.
        "KIMI_DISABLE_TELEMETRY" to "1",
    )

    /** `env -i KEY=VAL ...` prefix. Order is stable so tests can assert it. */
    fun envPrefix(env: List<Pair<String, String>> = agentEnv()): List<String> {
        val out = mutableListOf("/usr/bin/env", "-i")
        for ((k, v) in env) out.add("$k=$v")
        return out
    }

    /**
     * The full proot command line. [layout] supplies host paths, [extraEnv]
     * appends user-provided overrides (API key etc.) without touching the
     * defaults above — later assignments win in `env`.
     */
    fun buildCommand(layout: Layout, extraEnv: Map<String, String> = emptyMap()): List<String> {
        val binds = mutableListOf<String>()
        fun bind(host: File, guest: String) {
            binds.add("-b")
            binds.add("${host.absolutePath}:$guest")
        }

        val cmd = mutableListOf(layout.proot.absolutePath, "--kill-on-exit", "-0")
        cmd.add("-r")
        cmd.add(layout.rootfs.absolutePath)
        bind(layout.agentDir, GUEST_AGENT)
        bind(layout.depsDir, GUEST_SITE_PACKAGES)
        bind(layout.homeDir, GUEST_HOME)
        bind(layout.workspaceDir, GUEST_WORKSPACE)
        bind(layout.tmpDir, GUEST_TMP)
        // Kernel filesystems: proot only maps them, it does not create them.
        binds.add("-b"); binds.add("/proc")
        binds.add("-b"); binds.add("/dev")
        cmd.addAll(binds)
        cmd.add("-w")
        cmd.add(GUEST_WORKSPACE)
        cmd.addAll(envPrefix(agentEnv() + extraEnv.toList()))
        cmd.add(GUEST_PYTHON)
        cmd.add("-m")
        cmd.add("kimi_agent.cli")
        cmd.add("serve")
        return cmd
    }

    /** Files the unpacked runtime must contain before the agent can start. */
    fun requiredFiles(layout: Layout): List<File> = listOf(
        layout.proot,
        File(layout.rootfs, "usr/bin/python3.12"),
        // The archive carries /lib as a symlink to /usr/lib; check the real file
        // so a filesystem that refuses to create the link is not misread as a
        // broken runtime.
        File(layout.rootfs, "usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1"),
        File(layout.agentDir, "kimi_agent/cli.py"),
        File(layout.depsDir, "aiohttp/__init__.py"),
        layout.kimiBin,
    )
}

/** Host-side directory layout, taken from the app's private storage. */
class Layout(val filesDir: File, val nativeLibDir: File) {
    val root: File get() = File(filesDir, "runtime")
    val rootfs: File get() = File(root, "rootfs")
    val agentDir: File get() = File(root, "agent")
    val depsDir: File get() = File(root, "deps")
    val homeDir: File get() = File(root, "home")
    val workspaceDir: File get() = File(filesDir, "workspace")
    val tmpDir: File get() = File(root, "tmp")
    val logsDir: File get() = File(filesDir, "logs")

    // proot and kimi are shipped as jniLibs: Android refuses execve() on
    // anything inside the app's data dir for modern targets, and native libs
    // are the one location that is always executable.
    val proot: File get() = File(nativeLibDir, "libproot.so")
    val kimiStaged: File get() = File(rootfs, "usr/local/bin/kimi")
    val kimiBin: File get() = kimiStaged

    val readyMarker: File get() = File(root, ".ready-v1")

    fun ensureDirs() {
        listOf(root, agentDir, depsDir, homeDir, workspaceDir, tmpDir, logsDir).forEach {
            it.mkdirs()
        }
        File(rootfs, "usr/local/bin").mkdirs()
    }
}
