package net.hermes.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class FrameScalerTest {

    @Test
    fun `portrait image scales down to longest edge`() {
        val (width, height) = FrameScaler.computeScaledDimensions(1080, 1920, 1024)
        assertEquals(576, width)
        assertEquals(1024, height)
    }

    @Test
    fun `landscape image scales down to longest edge`() {
        val (width, height) = FrameScaler.computeScaledDimensions(1920, 1080, 1024)
        assertEquals(1024, width)
        assertEquals(576, height)
    }

    @Test
    fun `image already at or under cap is a no-op`() {
        val (width, height) = FrameScaler.computeScaledDimensions(800, 600, 1024)
        assertEquals(800, width)
        assertEquals(600, height)
    }

    @Test
    fun `longest edge exactly at cap is a no-op`() {
        val (width, height) = FrameScaler.computeScaledDimensions(1024, 512, 1024)
        assertEquals(1024, width)
        assertEquals(512, height)
    }

    @Test
    fun `never upscales below the cap`() {
        val (width, height) = FrameScaler.computeScaledDimensions(100, 50, 1024)
        assertEquals(100, width)
        assertEquals(50, height)
    }

    @Test
    fun `ladder tries quality steps before shrinking dimensions`() {
        val steps = FrameScaler.stepDownLadder().take(4).toList()

        // First two steps hold scale at 1.0 and only drop quality: 0.6 then 0.5.
        assertEquals(0.6, steps[0].quality, 0.0)
        assertEquals(1.0, steps[0].scale, 0.0)
        assertEquals(0.5, steps[1].quality, 0.0)
        assertEquals(1.0, steps[1].scale, 0.0)

        // Only once quality is floored at 0.5 does the ladder start shrinking dimensions.
        assertEquals(0.5, steps[2].quality, 0.0)
        assertTrue(steps[2].scale < 1.0)
        assertEquals(0.5, steps[3].quality, 0.0)
        assertTrue(steps[3].scale < steps[2].scale)
    }
}
