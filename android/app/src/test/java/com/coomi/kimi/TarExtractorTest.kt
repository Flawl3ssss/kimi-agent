package com.coomi.kimi

import java.io.ByteArrayInputStream
import java.io.File
import java.nio.file.Files
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unpacking is the one step with no second chance: a silently dropped entry
 * surfaces much later as "no such file" from inside the guest.
 *
 * The archive is a real GNU-tar file (see scripts/make_test_fixture.sh) rather
 * than hand-written headers, so long-name and padding behaviour is tested
 * against what GNU tar actually emits.
 */
class TarExtractorTest {

    /**
     * Read via the class-loader stream, not a File: AGP may expose unit-test
     * resources through a jar on the classpath, where no file path exists.
     */
    private val fixtureBytes: ByteArray by lazy {
        val loader = javaClass.classLoader ?: error("no class loader for unit tests")
        loader.getResourceAsStream("fixture.tar.gz")?.readBytes()
            ?: error("fixture.tar.gz missing from test resources")
    }

    private fun fixture(): java.io.InputStream = ByteArrayInputStream(fixtureBytes)

    private fun tempDir(prefix: String): File = Files.createTempDirectory(prefix).toFile()

    private val longName = "usr/lib/deep/nested/dir/" +
        "this_filename_is_definitely_longer_than_one_hundred_bytes_and_forces_a_gnu_long_name_header_record_in_the_tar_stream.txt"

    @Test
    fun extractsGnuArchiveIncludingLongNames() {
        val dest = tempDir("tarx")
        try {
            val result = fixture().use { TarExtractor.extractGz(it, dest) }

            assertEquals("long-name-content", File(dest, longName).readText())
            assertEquals("hello-file\n", File(dest, "usr/bin/echo.sh").readText())
            assertEquals("x", File(dest, "etc/plain.txt").readText())
            assertEquals(333L, File(dest, "payload.bin").length())
            assertTrue("files too low: ${result.summary()}", result.files >= 4)
            assertTrue("dirs too low: ${result.summary()}", result.dirs >= 5)
            // A symlink refusal (some CI filesystems) is acceptable; anything
            // else in a well-formed archive is a real regression.
            assertTrue("unexpected skips: ${result.skipped}",
                result.skipped.none { !it.startsWith("symlink") })
        } finally {
            dest.deleteRecursively()
        }
    }

    @Test
    fun staysSynchronisedAcrossAPartialBlock() {
        // 333 bytes = one full block plus a partial one: the classic place where
        // a reader forgets the padding and desynchronises every later entry.
        val dest = tempDir("tarx")
        try {
            fixture().use { TarExtractor.extractGz(it, dest) }
            assertEquals(333L, File(dest, "payload.bin").length())
            // Entries after it still landed, so padding was consumed exactly.
            assertTrue(File(dest, "usr/bin/echo.sh").exists())
            assertTrue(File(dest, longName).exists())
        } finally {
            dest.deleteRecursively()
        }
    }

    @Test
    fun executableBitSurvives() {
        val dest = tempDir("tarx")
        try {
            fixture().use { TarExtractor.extractGz(it, dest) }
            assertTrue(File(dest, "usr/bin/echo.sh").canExecute())
        } finally {
            dest.deleteRecursively()
        }
    }

    @Test
    fun refusesEntriesEscapingTheDestination() {
        val dest = tempDir("tarx")
        try {
            val evil = tarEntry("../evil.txt", "pwned".toByteArray(), '0')
            val result = ByteArrayInputStream(evil).use { TarExtractor.extract(it, dest) }
            assertFalse(File(dest.parentFile, "evil.txt").exists())
            assertFalse(File(dest, "evil.txt").exists())
            assertEquals(listOf("escapes destination: ../evil.txt"), result.skipped)
        } finally {
            dest.deleteRecursively()
        }
    }

    @Test
    fun paxRecordsAreSplitOnTheLengthPrefixNotTheFirstEquals() {
        // Real pax layout, exactly as GNU tar emits it: "<len> <key>=<value>\n",
        // len counting its own digits, the space and the newline. Values may
        // contain '=' — which is why the space, not the first '=', is the
        // boundary between length and record.
        assertEquals(mapOf("mtime" to "1789256975.108107076"),
            TarHeader.applyPax("30 mtime=1789256975.108107076\n"))
        assertEquals(mapOf("path" to "a=b.txt", "size" to "5"),
            TarHeader.applyPax("16 path=a=b.txt\n9 size=5\n"))
    }

    /** A one-file uncompressed tar: header block, padded payload, end marker. */
    private fun tarEntry(name: String, data: ByteArray, type: Char): ByteArray {
        val header = ByteArray(512)
        fun putAt(off: Int, s: String) = s.toByteArray().copyInto(header, off)
        putAt(0, name)
        putAt(100, "0000755")
        putAt(124, "%011o".format(data.size))
        putAt(148, "        ") // checksum field counts as spaces
        putAt(156, type.toString())
        putAt(257, "ustar  ")
        var sum = 0
        for (b in header) sum += b.toInt() and 0xff
        putAt(148, "%06o".format(sum))
        val padded = ByteArray(((data.size + 511) / 512) * 512)
        data.copyInto(padded)
        return header + padded + ByteArray(1024)
    }
}
