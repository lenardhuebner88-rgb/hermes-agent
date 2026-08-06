package net.hermes.dictate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class OverlayGeometryTest {
    // --- Active pill (listening/recording/processing/uploading — the wave is breathing) ---

    @Test fun `active pill on the operator device floors at the touch minimum, not the 60pc target`() {
        // 1080px/2.625 is the operator's own device. 60% of the screen is 648px, but the wave and
        // the cancel/confirm actions around it need at least 280dp (735px) to breathe — the floor
        // wins here, landing at 735px (~68%), still a large cut from the old 1008px (~93%).
        assertEquals(735, OverlayGeometry.pillWidthPx(1080, 2.625f, active = true))
    }

    @Test fun `active pill on a wide tablet is capped, not a banner`() {
        // 60% of a 1600px/2f tablet screen is 960px, above the 384dp cap (768px) — the cap wins.
        assertEquals(768, OverlayGeometry.pillWidthPx(1600, 2f, active = true))
    }

    @Test fun `active pill on a narrow viewport preserves margins instead of overflowing`() {
        // 60% of a 400px/2f screen is only 240px, below even the available-space floor (352px,
        // screen minus 2x12dp margins) — the available-space floor wins.
        assertEquals(352, OverlayGeometry.pillWidthPx(400, 2f, active = true))
    }

    // --- Quiet pill (done/cloud-done/failed — only as wide as what's actually visible) ---

    @Test fun `quiet pill with nothing to show shrinks to about a third of the screen`() {
        // Done without a visible message and without the undo window: just the two always-visible
        // circular actions plus their padding/margins, no content column reserved. On the
        // operator's device that is 346px, ~32% of the 1080px screen — the "leeres Feld" the
        // window used to show is gone because nothing is reserved for it any more.
        val width = OverlayGeometry.pillWidthPx(1080, 2.625f, active = false, contentVisible = false)
        assertEquals(346, width)
        assertTrue(width < 1080 / 3)
    }

    @Test fun `quiet pill widens for the undo window without reserving the content column`() {
        val width = OverlayGeometry.pillWidthPx(
            1080,
            2.625f,
            active = false,
            secondaryVisible = true,
            contentVisible = false,
        )
        // 132dp base + 48dp undo + 8dp gap = 188dp -> 493px. The gap is part of the width the
        // capsule must carry: without it the two buttons render flush and the last one clips.
        assertEquals(493, width)
    }

    @Test fun `quiet pill with a visible message reserves the content column for it`() {
        // CloudDone/Failed show a result or error line, so the content column is reserved even
        // though the state itself is not "active".
        val width = OverlayGeometry.pillWidthPx(1080, 2.625f, active = false, contentVisible = true)
        assertEquals(693, width)
    }

    @Test fun `quiet pill never shrinks below the touch targets it must still fit`() {
        // Recomputed independently from the raw dimens.xml tokens, not by calling back into
        // OverlayGeometry's own constants — otherwise this would just restate the formula.
        val density = 2.625f
        val touchTargetPx = (48 * density).toInt() // overlay_touch_target
        val horizontalPaddingPx = (8 * density).toInt() * 2 // pill_padding_horizontal, both sides
        val columnMarginPx = (10 * density).toInt() * 2 // pill_column_margin, both sides
        val cancelAndConfirmPx = touchTargetPx * 2
        val floorWithoutUndo = cancelAndConfirmPx + horizontalPaddingPx + columnMarginPx
        val floorWithUndo = floorWithoutUndo + touchTargetPx

        val quietNoUndo = OverlayGeometry.pillWidthPx(1080, density, active = false, contentVisible = false)
        val quietWithUndo = OverlayGeometry.pillWidthPx(
            1080,
            density,
            active = false,
            secondaryVisible = true,
            contentVisible = false,
        )

        assertTrue("quiet pill must still fit cancel+confirm", quietNoUndo >= floorWithoutUndo)
        assertTrue("quiet pill must still fit cancel+undo+confirm", quietWithUndo >= floorWithUndo)
    }

    // --- Shared height token ---

    @Test fun `height is one stable token shared with the layout`() {
        assertEquals(168, OverlayGeometry.pillHeightPx(2.625f))
    }
}
