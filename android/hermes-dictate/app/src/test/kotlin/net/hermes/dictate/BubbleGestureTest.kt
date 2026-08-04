package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class BubbleGestureTest {
    @Test
    fun `movement inside system slop remains a tap`() {
        val gesture = BubbleGesture(touchSlopPx = 16f)
        gesture.begin(100f, 100f)
        assertFalse(gesture.move(108f, 108f))
        assertEquals(BubbleGesture.Finish.TAP, gesture.finish(cancelled = false, longPressed = false))
    }

    @Test
    fun `diagonal movement beyond slop becomes a drag and stays one`() {
        val gesture = BubbleGesture(touchSlopPx = 16f)
        gesture.begin(100f, 100f)
        assertTrue(gesture.move(113f, 113f))
        assertTrue(gesture.move(101f, 101f))
        assertEquals(BubbleGesture.Finish.DRAG, gesture.finish(cancelled = false, longPressed = false))
    }

    @Test
    fun `cancel never snaps or triggers a tap even after movement`() {
        val gesture = BubbleGesture(touchSlopPx = 8f)
        gesture.begin(0f, 0f)
        gesture.move(40f, 0f)
        assertEquals(BubbleGesture.Finish.CANCEL, gesture.finish(cancelled = true, longPressed = false))
    }

    @Test
    fun `long press wins over tap`() {
        val gesture = BubbleGesture(touchSlopPx = 16f)
        gesture.begin(0f, 0f)
        assertEquals(BubbleGesture.Finish.LONG_PRESS, gesture.finish(cancelled = false, longPressed = true))
    }
}
