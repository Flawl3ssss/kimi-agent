package com.coomi.kimi

import java.io.File
import java.io.FileInputStream

/**
 * Standalone driver: unpack one of the shipped payload archives with the exact
 * Kotlin reader the app uses, then print what came out.
 *
 * CI only exercises a 4 KB fixture, which proves the format handling but not
 * that the real 67 MB rootfs survives. On the phone there is no second chance:
 * a desynchronised stream looks like a corrupt image after minutes of unpacking.
 *
 * Usage: kotlinc ... && java -cp ... com.coomi.kimi.TarCheck <archive> <dest>
 */
object TarCheck {
    @JvmStatic
    fun main(args: Array<String>) {
        val archive = File(args.getOrNull(0) ?: error("usage: TarCheck <archive.tar.gz[.bin]> <dest>"))
        val dest = File(args.getOrNull(1) ?: error("usage: TarCheck <archive> <dest>"))
        dest.mkdirs()
        val t0 = System.currentTimeMillis()
        var count = 0
        val progress = { s: String -> if (++count % 2000 == 0) println("  ... $s") }
        val result = FileInputStream(archive).use {
            TarExtractor.extractGz(it, dest) { msg -> println("log: $msg"); progress(msg) }
        }
        val secs = (System.currentTimeMillis() - t0) / 1000
        println("summary: ${result.summary()} in ${secs}s")
        println("bytes written: ${dest.walkTopDown().filter { it.isFile }.sumOf { it.length() }}")
        if (result.skipped.isNotEmpty()) {
            println("SKIPPED (first 10):")
            result.skipped.take(10).forEach { println("  $it") }
        }
        // Exit non-zero on anything skipped: for these archives there is no
        // legitimate skip, so a non-zero exit is always a regression.
        val bad = result.skipped.filterNot { it.startsWith("symlink") }
        if (bad.isNotEmpty()) {
            System.err.println("FAIL: ${bad.size} unexpected skips")
            kotlin.system.exitProcess(1)
        }
        println("OK")
    }
}
