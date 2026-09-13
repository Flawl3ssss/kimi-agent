package com.coomi.kimi

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.util.zip.GZIPInputStream

/**
 * Minimal POSIX/GNU tar reader.
 *
 * Needed because the archives must be unpacked *before* any guest binary
 * exists: asking proot to extract its own rootfs is circular, and Android's
 * toybox tar does not handle every header GNU tar writes (long names, pax).
 *
 * Handles regular files, directories, symlinks and hardlinks; GNU 'L' and pax
 * 'x' extended headers are honoured. Unsupported entries are counted and
 * reported, never dropped silently.
 */
object TarExtractor {

    class Result(
        val files: Int,
        val dirs: Int,
        val links: Int,
        val skipped: List<String>,
    ) {
        fun summary(): String = "files=$files dirs=$dirs links=$links skipped=${skipped.size}"
    }

    fun extractGz(input: InputStream, dest: File, log: (String) -> Unit = {}): Result =
        GZIPInputStream(BufferedInputStream(input, 1 shl 16)).use { extract(it, dest, log) }

    fun extract(raw: InputStream, dest: File, log: (String) -> Unit = {}): Result {
        val tar = TarReader(raw)
        var files = 0
        var dirs = 0
        var links = 0
        val skipped = mutableListOf<String>()

        while (true) {
            var hdr = tar.readHeader() ?: break
            // Apply any GNU 'L' / pax overrides captured before this header.
            // Local vals: pendingName/pendingSize belong to another object, so
            // Kotlin refuses to smart-cast them even after the null check.
            val pendingName = tar.pendingName
            val pendingSize = tar.pendingSize
            if (pendingName != null) hdr = hdr.copy(name = pendingName)
            if (pendingSize != null) hdr = hdr.copy(size = pendingSize)
            tar.pendingName = null
            tar.pendingSize = null
            when (hdr.type) {
                // Extended headers describe the *next* entry.
                TarHeader.LONG_NAME, TarHeader.PAX -> {
                    val payload = tar.readPayloadString(hdr.size)
                    if (hdr.type == TarHeader.LONG_NAME) {
                        tar.pendingName = payload
                    } else {
                        TarHeader.applyPax(payload).let { pax ->
                            pax["path"]?.let { tar.pendingName = it }
                            pax["size"]?.toLongOrNull()?.let { tar.pendingSize = it }
                        }
                    }
                    continue
                }
            }

            val name = hdr.name
            val target = resolve(dest, name)
            if (target == null) {
                tar.skipBlocks(hdr.size)
                skipped += "escapes destination: $name"
                continue
            }

            when (hdr.type) {
                TarHeader.DIR -> {
                    tar.skipBlocks(hdr.size)
                    if (target.isDirectory || target.mkdirs()) dirs++ else skipped += "mkdir $name"
                }
                TarHeader.FILE, TarHeader.FILE_ALT -> {
                    target.parentFile?.mkdirs()
                    FileOutputStream(target).use { out ->
                        BufferedOutputStream(out, 1 shl 16).use { tar.copyPayload(it, hdr.size) }
                    }
                    if (hdr.mode and 0b111 != 0) target.setExecutable(true, true)
                    target.setReadable(true, false)
                    files++
                }
                TarHeader.SYMLINK -> {
                    tar.skipBlocks(hdr.size)
                    if (hdr.linkName.isEmpty()) {
                        skipped += "symlink without target: $name"
                    } else {
                        val made = runCatching {
                            target.delete()
                            java.nio.file.Files.createSymbolicLink(
                                target.toPath(), java.nio.file.Paths.get(hdr.linkName)
                            )
                            true
                        }.getOrDefault(false)
                        if (made) links++
                        else skipped += "symlink $name -> ${hdr.linkName} (filesystem refused)"
                    }
                }
                TarHeader.HARDLINK -> {
                    tar.skipBlocks(hdr.size)
                    val src = resolve(dest, hdr.linkName)
                    val made = src != null && src.isFile && runCatching {
                        target.parentFile?.mkdirs()
                        java.nio.file.Files.copy(src.toPath(), target.toPath(),
                            java.nio.file.StandardCopyOption.REPLACE_EXISTING)
                        true
                    }.getOrDefault(false)
                    if (made) files++ else skipped += "hardlink $name -> ${hdr.linkName}"
                }
                else -> {
                    tar.skipBlocks(hdr.size)
                    if (name.isNotEmpty()) skipped += "type '${hdr.type}' $name"
                }
            }
        }

        if (skipped.isNotEmpty()) {
            log("tar: skipped ${skipped.size} entr(y/ies), e.g. ${skipped.take(3).joinToString(" | ")}")
        }
        return Result(files, dirs, links, skipped)
    }

    /** Refuse anything that would write outside [dest]. */
    private fun resolve(dest: File, entry: String): File? {
        val clean = entry.removePrefix("./").removePrefix("/")
        if (clean.isEmpty() || clean == ".") return dest
        val base = runCatching { dest.canonicalFile.path }.getOrNull() ?: return null
        val candidate = File(dest, clean)
        val canon = runCatching { candidate.canonicalFile.path }.getOrNull() ?: return null
        return if (canon == base || canon.startsWith(base + File.separator)) candidate else null
    }
}
