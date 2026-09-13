package com.coomi.kimi

import java.io.IOException
import java.io.InputStream

/** One 512-byte tar header record. */
data class TarHeader(
    val name: String,
    val mode: Int,
    val size: Long,
    val type: Char,
    val linkName: String,
) {
    companion object {
        const val FILE = '0'
        const val FILE_ALT = '\u0000'
        const val DIR = '5'
        const val SYMLINK = '2'
        const val HARDLINK = '1'
        const val LONG_NAME = 'L'   // GNU: name of the next entry
        const val PAX = 'x'         // pax extended header for the next entry
        const val PAX_GLOBAL = 'g'

        /**
         * Parse a pax extended header: `<len> <key>=<value>\n`, where `<len>`
         * counts the whole record including itself and the newline.
         *
         * The length prefix, not the first `=`, is the record boundary — values
         * may contain `=` and the space after the length is mandatory.
         */
        fun applyPax(payload: String): Map<String, String> {
            val out = mutableMapOf<String, String>()
            var i = 0
            while (i < payload.length) {
                val sp = payload.indexOf(' ', i)
                if (sp < 0) break
                val len = payload.substring(i, sp).trim().toIntOrNull() ?: break
                if (len <= 0) break
                val record = payload.substring(sp + 1, minOf(payload.length, i + len))
                val sep = record.indexOf('=')
                if (sep > 0) out[record.substring(0, sep)] = record.substring(sep + 1).trimEnd('\n')
                i += len
            }
            return out
        }
    }
}

/** Block-oriented reader: keeps stream position exact, which tar correctness needs. */
class TarReader(private val raw: InputStream) {
    var pendingName: String? = null
    var pendingSize: Long? = null

    private val block = ByteArray(512)

    private fun readFully(out: ByteArray, off: Int, len: Int): Int {
        var got = 0
        while (got < len) {
            val n = raw.read(out, off + got, len - got)
            if (n < 0) break
            got += n
        }
        return got
    }

    /** Null at a clean end of archive, otherwise the header. */
    fun readHeader(): TarHeader? {
        val n = readFully(block, 0, 512)
        if (n < 512) return null
        if (block.all { it == 0.toByte() }) return null // end-of-archive marker
        val name = cstr(0, 100)
        val size = octal(124, 12).let { if (it < 0) 0 else it }
        return TarHeader(
            name = name,
            mode = octal(100, 8).toInt(),
            size = size,
            type = block[156].toInt().toChar(),
            linkName = cstr(157, 100),
        )
    }

    fun readPayloadString(size: Long): String {
        val cap = size.coerceAtMost(1L shl 20).toInt()
        val out = ByteArray(cap)
        readFully(out, 0, cap)
        // Only the padding is left to skip: skipBlocks(size) would also re-skip
        // the `cap` bytes just read and land us one block into the next header.
        skipPadding(size, cap.toLong())
        return String(out, Charsets.UTF_8).trim('\u0000', ' ', '\n')
    }

    fun copyPayload(out: java.io.OutputStream, size: Long) {
        val chunk = ByteArray(1 shl 16)
        var remaining = size
        while (remaining > 0) {
            val want = minOf(remaining, chunk.size.toLong()).toInt()
            val n = readFully(chunk, 0, want)
            if (n <= 0) throw IOException("truncated tar entry (wanted $remaining)")
            out.write(chunk, 0, n)
            remaining -= n
        }
        out.flush()
        val consumed = size
        val padded = ((consumed + 511) / 512) * 512
        var pad = padded - consumed
        while (pad > 0) {
            val n = raw.skip(pad)
            if (n <= 0) {
                if (raw.read() < 0) return
                pad -= 1
            } else pad -= n
        }
    }

    fun skipBlocks(size: Long) {
        var left = ((size + 511) / 512) * 512
        while (left > 0) {
            val n = raw.skip(left)
            if (n <= 0) {
                if (raw.read() < 0) return
                left -= 1
            } else left -= n
        }
    }

    /** Skip the block padding that follows `read` already-consumed bytes. */
    private fun skipPadding(size: Long, alreadyRead: Long) {
        var left = ((size + 511) / 512) * 512 - alreadyRead
        while (left > 0) {
            val n = raw.skip(left)
            if (n <= 0) {
                if (raw.read() < 0) return
                left -= 1
            } else left -= n
        }
    }

    private fun cstr(off: Int, len: Int): String {
        var end = off
        while (end < off + len && block[end] != 0.toByte()) end++
        return String(block, off, end - off, Charsets.UTF_8).trim()
    }

    private fun octal(off: Int, len: Int): Long {
        val s = String(block, off, len, Charsets.ISO_8859_1)
            .trim('\u0000', ' ')
            .trimEnd('\u0000', ' ')
        if (s.isEmpty()) return 0L
        if (s.length == 1 && s[0] == ' ') return 0L
        return runCatching { java.lang.Long.parseUnsignedLong(s, 8) }.getOrDefault(-1L)
    }
}
