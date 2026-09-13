package com.coomi.kimi

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [RuntimeSpec] is the single definition of how the guest is launched;
 * AgentService builds its argv from here, so these assertions describe exactly
 * the command that will run on the phone.
 */
class RuntimeSpecTest {

    private val layout = Layout(File("/data/user/0/com.coomi.kimi/files"), File("/data/app/x/lib"))

    private fun argv(extra: Map<String, String> = emptyMap()) =
        RuntimeSpec.buildCommand(layout, extra)

    @Test
    fun rootfsIsTheFirstBindSoItBecomesGuestRoot() {
        val a = argv()
        val i = a.indexOf("-r")
        assertTrue("-r missing", i > 0)
        assertEquals(layout.rootfs.absolutePath, a[i + 1])
        // proot applies -b flags in order; the guest-visible / must come from rootfs.
        assertEquals(layout.agentDir.absolutePath + ":/opt/agent", a[a.indexOf("-b") + 1])
    }

    @Test
    fun everyGuestMountIsDeclaredBeforeTheCommand() {
        val a = argv()
        val guests = a.withIndex().filter { it.value == "-b" }.map { a[it.index + 1].substringAfter(':', "") }
        assertTrue(guests.toString(), guests.containsAll(listOf("/opt/agent", "/opt/deps", "/home/coomi", "/workspace", "/tmp")))
        // Kernel filesystems are bound by path only.
        assertTrue(guests.toString(), guests.contains("/proc") || a.contains("/proc"))
        assertEquals("/workspace", a[a.indexOf("-w") + 1])
    }

    @Test
    fun environmentIsWipedThenSetExplicitly() {
        val a = argv()
        val envIdx = a.indexOf("/usr/bin/env")
        assertTrue("env -i must be used", envIdx > 0 && a[envIdx + 1] == "-i")
        val pairs = a.drop(envIdx + 2).takeWhile { it.contains('=') && !it.startsWith("/") }
            .map { it.substringBefore('=') to it.substringAfter('=') }
        val env = pairs.toMap()
        assertEquals("/home/coomi", env["HOME"])
        assertEquals("/opt/agent:/opt/deps", env["PYTHONPATH"])
        assertEquals("/tmp", env["TMPDIR"])
        assertEquals("8765", env["COOMI_KIMI_PORT"])
        // The venv is not shipped: absolute interpreter paths would break.
        assertEquals("/opt/agent:/opt/deps", env["PYTHONPATH"])
        assertEquals(RuntimeSpec.GUEST_PYTHON, a[a.indexOf("-m") - 1])
        assertEquals(listOf("-m", "kimi_agent.cli", "serve"), a.takeLast(3))
    }

    @Test
    fun callerEnvironmentOverridesAreAppendedNotReplaced() {
        val a = argv(mapOf("COOMI_ANDROID" to "1", "COOMI_VERSION_NAME" to "0.5.0"))
        assertTrue(a.contains("COOMI_ANDROID=1"))
        assertTrue(a.contains("COOMI_VERSION_NAME=0.5.0"))
        // Later assignments win in env(1), so defaults must come first.
        assertTrue(a.indexOf("HOME=/home/coomi") < a.indexOf("COOMI_ANDROID=1"))
    }

    @Test
    fun hostPathsNeverReachTheGuestCommandLine() {
        val joined = argv().joinToString(" ")
        // Only the -r/-b sources and the proot binary may contain host paths.
        val guest = joined.split("-b").last()
        assertFalse("host data path leaked: $guest", guest.contains("/data/user/0"))
    }

    @Test
    fun requiredFilesAreTheRealPreconditions() {
        val names = RuntimeSpec.requiredFiles(layout).map { it.path }
        assertTrue(names.toString(), names.any { it.endsWith("libproot.so") })
        assertTrue(names.toString(), names.any { it.endsWith("usr/bin/python3.12") })
        assertTrue(names.toString(), names.any { it.endsWith("ld-linux-aarch64.so.1") })
        assertTrue(names.toString(), names.any { it.endsWith("kimi_agent/cli.py") })
        assertTrue(names.toString(), names.any { it.endsWith("aiohttp/__init__.py") })
        assertTrue(names.toString(), names.any { it.endsWith("usr/local/bin/kimi") })
    }

    @Test
    fun prootAndKimiComeFromTheNativeLibDir() {
        // Android blocks execve() inside app data for targetSdk >= 29; jniLibs is
        // the one always-executable location, which is why these two are .so.
        assertEquals(File(layout.nativeLibDir, "libproot.so"), layout.proot)
        assertEquals(File(layout.rootfs, "usr/local/bin/kimi"), layout.kimiBin)
    }

    @Test
    fun agentEnvOrderIsStable() {
        val first = RuntimeSpec.agentEnv().map { it.first }
        assertEquals(first, RuntimeSpec.agentEnv().map { it.first })
        assertTrue(first.contains("SSL_CERT_FILE"))
    }
}
