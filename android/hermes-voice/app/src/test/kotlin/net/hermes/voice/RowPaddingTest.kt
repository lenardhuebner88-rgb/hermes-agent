package net.hermes.voice

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class RowPaddingTest {

    private val sentinel = 0xEE.toByte()

    @Test
    fun `stride equal to tight row size is a passthrough`() {
        val width = 3
        val height = 2
        val pixelStride = 4
        val rowStride = width * pixelStride
        val src = ByteArray(rowStride * height) { it.toByte() }

        val out = RowPadding.stripRowPadding(src, width, height, rowStride, pixelStride)

        assertArrayEquals(src, out)
    }

    @Test
    fun `stride greater than tight row size strips padding`() {
        val width = 3
        val height = 2
        val pixelStride = 4
        val tightRowBytes = width * pixelStride // 12
        val rowStride = 16 // 4 padding bytes per row

        val src = ByteArray(rowStride * height)
        // Row 0 pixel bytes: 0..11, then sentinel padding.
        for (i in 0 until tightRowBytes) src[i] = i.toByte()
        for (i in tightRowBytes until rowStride) src[i] = sentinel
        // Row 1 pixel bytes: 100..111, then sentinel padding.
        val row1Start = rowStride
        for (i in 0 until tightRowBytes) src[row1Start + i] = (100 + i).toByte()
        for (i in tightRowBytes until rowStride) src[row1Start + i] = sentinel

        val out = RowPadding.stripRowPadding(src, width, height, rowStride, pixelStride)

        val expected = ByteArray(tightRowBytes * height)
        for (i in 0 until tightRowBytes) expected[i] = i.toByte()
        for (i in 0 until tightRowBytes) expected[tightRowBytes + i] = (100 + i).toByte()

        assertArrayEquals(expected, out)
        assertFalse("sentinel padding byte leaked into stripped output", out.any { it == sentinel })
    }

    @Test
    fun `buffer sized to exactly reach the last pixel of the last row is accepted`() {
        // Image.Plane only guarantees the buffer reaches the last pixel of the last row:
        // (height - 1) * rowStride + width * pixelStride, NOT a full rowStride * height — there
        // is no trailing padding requirement after the final row's pixel data.
        val width = 3
        val height = 2
        val pixelStride = 4
        val tightRowBytes = width * pixelStride // 12
        val rowStride = 16 // 4 padding bytes per row
        val minValidSize = (height - 1) * rowStride + tightRowBytes // 28

        val src = ByteArray(minValidSize)
        // Row 0 pixel bytes: 0..11, then sentinel padding.
        for (i in 0 until tightRowBytes) src[i] = i.toByte()
        for (i in tightRowBytes until rowStride) src[i] = sentinel
        // Row 1 (last row): pixel bytes only, buffer ends right after — no trailing padding.
        val row1Start = rowStride
        for (i in 0 until tightRowBytes) src[row1Start + i] = (100 + i).toByte()

        val out = RowPadding.stripRowPadding(src, width, height, rowStride, pixelStride)

        val expected = ByteArray(tightRowBytes * height)
        for (i in 0 until tightRowBytes) expected[i] = i.toByte()
        for (i in 0 until tightRowBytes) expected[tightRowBytes + i] = (100 + i).toByte()

        assertArrayEquals(expected, out)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `buffer one byte short of the last-row minimum is rejected`() {
        val width = 3
        val height = 2
        val pixelStride = 4
        val tightRowBytes = width * pixelStride
        val rowStride = 16
        val minValidSize = (height - 1) * rowStride + tightRowBytes
        val src = ByteArray(minValidSize - 1)

        RowPadding.stripRowPadding(src, width, height, rowStride, pixelStride)
    }
}
