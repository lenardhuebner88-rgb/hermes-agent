package net.hermes.dictate

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * `OverlayGeometry` computes in pixels and cannot read resources, so the pill height exists twice:
 * as a Kotlin constant and as `@dimen/pill_height`. This test reads the actual resource file and
 * fails when the two drift apart — the drift is otherwise silent and only visible on a device.
 */
class OverlayGeometryTokenTest {

    private fun resourceDir(): File {
        var current: File? = File(System.getProperty("user.dir").orEmpty()).absoluteFile
        while (current != null) {
            val candidate = File(current, "app/src/main/res")
            if (candidate.isDirectory) return candidate
            val direct = File(current, "src/main/res")
            if (direct.isDirectory) return direct
            current = current.parentFile
        }
        throw AssertionError("could not locate app/src/main/res from ${System.getProperty("user.dir")}")
    }

    private fun dimen(name: String): Int {
        val text = File(resourceDir(), "values/dimens.xml").readText()
        val match = Regex("<dimen name=\"$name\">(\\d+)(?:dp|sp)</dimen>").find(text)
            ?: throw AssertionError("dimen $name is missing from values/dimens.xml")
        return match.groupValues[1].toInt()
    }

    @Test
    fun `pill height constant matches the layout token`() {
        assertEquals(OverlayGeometry.PILL_HEIGHT_DP, dimen("pill_height"))
    }

    @Test
    fun `touch targets stay at or above the android minimum`() {
        assertTrue("48dp is the Android minimum touch target", dimen("overlay_touch_target") >= 48)
        assertTrue("48dp is the Android minimum touch target", dimen("bubble_size") >= 48)
    }

    @Test
    fun `the level bar fits inside the pill content band`() {
        assertTrue(dimen("pill_content_height") < dimen("pill_height"))
        assertTrue(dimen("pill_level_height") <= dimen("pill_height") - dimen("pill_content_height"))
    }

    @Test
    fun `overlay layouts carry no hard coded measurements`() {
        val literal = Regex("\"\\d+(?:dp|sp)\"")
        for (name in listOf("overlay_pill.xml", "overlay_bubble.xml")) {
            val text = File(resourceDir(), "layout/$name").readText()
            val offenders = literal.findAll(text).map { it.value }.filter { it != "\"0dp\"" }.toList()
            assertEquals("$name must reference @dimen tokens", emptyList<String>(), offenders)
        }
    }

    @Test
    fun `overlay drawables reference color resources instead of literals`() {
        val literal = Regex("android:(?:fillColor|color)=\"#")
        for (name in listOf("bg_pill.xml", "bg_bubble.xml", "bg_chip.xml", "bg_hermes_action.xml")) {
            val text = File(resourceDir(), "drawable/$name").readText()
            assertTrue("$name must use @color tokens", literal.find(text) == null)
        }
    }
}
