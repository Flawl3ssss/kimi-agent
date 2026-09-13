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
        healDepsPrefix()

        // ".bin" is not cosmetic: aapt2 gunzips assets whose name ends in ".gz"
        // and strips the extension, so "rootfs.tar.gz" reached the device as an
        // uncompressed "rootfs.tar" while Bootstrap still opens it as gzip.
        for ((asset, dest, label) in listOf(
            Triple("rootfs.tar.gz.bin", layout.rootfs, "Распаковка Ubuntu rootfs…"),
            Triple("deps.tar.gz.bin", layout.depsDir, "Распаковка Python-библиотек…"),
            Triple("app.tar.gz.bin", layout.agentDir, "Распаковка агента…"),
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

    /**
     * The first shipped APK packed deps.tar.gz.bin with a "deps/" prefix, so the
     * libraries landed in root/deps/deps and startup failed on
     * "missing after unpack: __init__.py". The archive is fixed, but extract()
     * is marker-idempotent: an already-installed device would keep the broken
     * tree. Drop the nested copy and its marker, so the corrected archive is
     * unpacked in its place. Nothing is *moved* up instead: a copy whose source
     * lives inside its destination (deps/deps -> deps) never terminates.
     */
    private fun healDepsPrefix() {
        val nested = File(layout.depsDir, "deps")
        if (!nested.isDirectory) return
        log("deps: removing nested deps/deps from the first build")
        if (!runCatching { nested.deleteRecursively() }.isSuccess) {
            log("deps heal: delete failed, leaving marker so extract rewrites files")
        }
        // Always re-extract: the marker proves only that *something* unpacked
        // here, and for the first build that something had the wrong shape.
        File(layout.depsDir, ".extracted-deps.tar.gz.bin").delete()
    }

    /**
     * Expand `assets/<name>` into [dest].
     *
     * The marker stores the asset's byte size, not just "done": payload archives
     * get re-packed between builds (the deps/ prefix fix, the UI fixes) while the
     * extraction summary stays identical, so a size-blind marker would keep an
     * installed device on stale files forever. Markers from the first build carry
     * no size and are therefore re-extracted once, then become stable.
     */
    private fun extract(asset: String, dest: File): Boolean {
        val marker = File(dest, ".extracted-$asset")
        val assetSize = runCatching {
            ctx.assets.openFd(asset).use { it.length }
        }.getOrElse { -1L }
        if (marker.isFile && assetSize > 0) {
            val recorded = Regex("size=(\\d+)").find(
                runCatching { marker.readText() }.getOrElse { "" })?.groupValues?.get(1)?.toLong()
            if (recorded == assetSize) return true
        } else if (marker.isFile) {
            return true // size unknown (compressed asset); trust the existing marker
        }
        val t0 = System.currentTimeMillis()
        return runCatching {
            dest.mkdirs()
            val res = ctx.assets.open(asset).use { input ->
                TarExtractor.extractGz(input, dest) { log("tar $asset: $it") }
            }
            log("$asset -> ${res.summary()} in ${(System.currentTimeMillis() - t0) / 1000}s")
            marker.parentFile?.mkdirs()
            marker.writeText("size=$assetSize ${res.summary()}\n")
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
