package net.hermes.voice

/**
 * Strips ImageReader row padding (rowStride > width * pixelStride) from a raw pixel buffer,
 * producing a tight (no gaps) byte layout. Pure ByteArray/Int math — JVM-testable.
 */
object RowPadding {

    /**
     * Copies [src] (laid out as [height] rows of [rowStride] bytes each) into a tight buffer
     * of [height] rows of `width * pixelStride` bytes each, dropping the trailing padding
     * bytes of every row. If there is no padding ([rowStride] already equals the tight row
     * size), this is a straight copy.
     */
    fun stripRowPadding(
        src: ByteArray,
        width: Int,
        height: Int,
        rowStride: Int,
        pixelStride: Int,
    ): ByteArray {
        require(width > 0 && height > 0) { "dimensions must be positive" }
        require(pixelStride > 0) { "pixelStride must be positive" }
        val tightRowBytes = width * pixelStride
        require(rowStride >= tightRowBytes) { "rowStride must be >= width * pixelStride" }
        // Image.Plane only guarantees the buffer reaches the last pixel of the last row, i.e.
        // (height - 1) full strides plus one tight row — NOT a full stride for every row
        // (there is no trailing padding requirement after the final row). Requiring
        // rowStride * height rejects legally-sized buffers real devices hand back.
        val minValidSize = (height - 1) * rowStride + tightRowBytes
        require(src.size >= minValidSize) {
            "src too small: must reach the last pixel of the last row " +
                "((height-1)*rowStride + width*pixelStride)"
        }

        if (rowStride == tightRowBytes) {
            return src.copyOf(tightRowBytes * height)
        }

        val out = ByteArray(tightRowBytes * height)
        for (row in 0 until height) {
            System.arraycopy(src, row * rowStride, out, row * tightRowBytes, tightRowBytes)
        }
        return out
    }
}
