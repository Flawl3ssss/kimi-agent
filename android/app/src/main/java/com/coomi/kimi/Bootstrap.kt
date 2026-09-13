package com.coomi.kimi

import android.content.Context
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream

/**
 * Unpacks the shipped payload into [Layout] exactly once.
 *
 * The three archives are expanded with [TarExtractor] rather than by shelling
 * out to `proot tar`, which would be circular: proot cannot run until the rootfs
 * it is supposed to extract already exists.
 *
 * `libkimi.so` is the Kimi Code binary, shipped as a jniLib because that is the
 * only location Android always allows to be read as an ordinary file; the agent
 * expects it inside the rootfs, so it is copied there.
 *
 * Nothing is marked ready until every step succeeded — a half-extracted rootfs
 * is worse than none, and the marker makes a failure retry from scratch.
 */
class Bootstrap(private val ctx: Context, private val log: (String) -> Unit) {

    // applicationInfo.nativeLibraryDir is a String path, not a File.
    private val layout = Layout(ctx.filesDir, File(ctx.applicationInfo.nativeLibraryDir))

    fun layout(): Layout = layout

    fun isReady(): Boolean =
        layout.readyMarker.isFile && RuntimeSpec.requiredFiles(layout).all { it.exists() }

    fun prepare(onProgress: (String) -> Unit): Boolean {
        layout.ensureDirs()
        if (isReady()) {
            log("runtime already unpacked")
            return true
        }
        layout.readyMarker.delete()

        for ((asset, dest, label) in listOf(
            Triple("rootfs.tar.gz", layout.rootfs, "Распаковка Ubuntu rootfs…"),
            Triple("deps.tar.gz", layout.depsDir, "Распаковка Python-библиотек…"),
            Triple("app.tar.gz", layout.agentDir, "Распаковка агента…"),
        )) {
            onProgress(label)
            if (!extract(asset, dest)) return false
        }

        onProgress("Установка Kimi Code…")
        if (!stageKimi()) return false

        val missing = RuntimeSpec.requiredFiles(layout).filterNot { it.exists() }
        if (missing.isNotEmpty()) {
            log("missing after unpack: ${missing.joinToString { it.name }}")
            return false
        }
        layout.readyMarker.parentFile?.mkdirs()
        layout.readyMarker.writeText("ok ${System.currentTimeMillis()}\n")
        log("runtime ready")
        return true
    }

    /** Expand `assets/<name>` into [dest]; a per-archive marker keeps it idempotent. */
    private fun extract(asset: String, dest: File): Boolean {
        val marker = File(dest, ".extracted-$asset")
        if (marker.isFile) return true
        val t0 = System.currentTimeMillis()
        return runCatching {
            dest.mkdirs()
            val res = ctx.assets.open(asset).use { input ->
                TarExtractor.extractGz(input, dest) { log("tar $asset: $it") }
            }
            log("$asset -> ${res.summary()} in ${(System.currentTimeMillis() - t0) / 1000}s")
            marker.parentFile?.mkdirs()
            marker.writeText(res.summary() + "\n")
            true
        }.getOrElse {
            log("extract $asset failed: ${it.message}")
            false
        }
    }

    /**
     * Copy the Kimi Code binary into the rootfs, where run.sh and the agent's
     * `COOMI_KIMI_BIN` look for it. Skipped when the file is already there with
     * the right size, so a restart does not rewrite 160 MB.
     */
    private fun stageKimi(): Boolean {
        val src = File(layout.nativeLibDir, "libkimi.so")
        if (!src.isFile) {
            log("libkimi.so not found in ${layout.nativeLibDir}")
            return false
        }
        val dst = File(layout.rootfs, "usr/local/bin/kimi")
        if (dst.isFile && dst.length() == src.length()) {
            dst.setExecutable(true, true)
            return true
        }
        dst.parentFile?.mkdirs()
        return runCatching {
            FileInputStream(src).use { input ->
                // Write to a temp name first: a crash mid-copy must not leave a
                // truncated binary that looks valid by size on the next start.
                val tmp = File(dst.parentFile, "kimi.part")
                FileOutputStream(tmp).use { out -> input.copyTo(out, 1 shl 20) }
                tmp.renameTo(dst)
            }
            dst.setExecutable(true, true)
            dst.setReadable(true, false)
            log("kimi staged: ${dst.length()} bytes")
            true
        }.getOrElse {
            log("staging kimi failed: ${it.message}")
            false
        }
    }

    /** Free the unpacked runtime (used by the "reset runtime" action). */
    fun wipe(): Boolean {
        layout.readyMarker.delete()
        listOf(layout.agentDir, layout.depsDir, layout.rootfs).forEach { runCatching { it.deleteRecursively() } }
        return !layout.rootfs.exists()
    }
}
