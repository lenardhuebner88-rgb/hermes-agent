package net.hermes.deck.model

import java.time.LocalDate
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class DueDatesTest {

    // A fixed date, never LocalDate.now(): a test whose expectations depend on
    // the day it runs is green at review and red later.
    private val today = LocalDate.of(2026, 8, 5)

    @Test
    fun `writes and reads back ISO`() {
        assertEquals("2026-08-05", DueDates.format(today))
        assertEquals(today, DueDates.parse("2026-08-05"))
    }

    @Test
    fun `an unparseable date from the channel is no date, not an exception`() {
        // The tag is written by anything posting into the channel, not only by
        // this app, so a value it never wrote has to pass through harmlessly.
        assertNull(DueDates.parse("naechste Woche"))
        assertNull(DueDates.parse("2026-13-40"))
        assertNull(DueDates.parse(""))
        assertNull(DueDates.parse(null))
        assertNull(DueDates.label("irgendwann", today))
        assertFalse(DueDates.isOverdue("irgendwann", today))
    }

    @Test
    fun `parse tolerates surrounding blanks`() {
        assertEquals(today, DueDates.parse("  2026-08-05 "))
    }

    @Test
    fun `labels are relative where relative is clearer`() {
        assertEquals("Heute", DueDates.label("2026-08-05", today))
        assertEquals("Morgen", DueDates.label("2026-08-06", today))
        assertEquals("Gestern", DueDates.label("2026-08-04", today))
        // Two to six days out: the weekday alone is unambiguous.
        assertEquals("Fr", DueDates.label("2026-08-07", today))
        assertEquals("Mo", DueDates.label("2026-08-10", today))
        // Beyond the week a weekday would be ambiguous, so the date wins.
        assertEquals("15.08.", DueDates.label("2026-08-15", today))
        assertEquals("28.07.", DueDates.label("2026-07-28", today))
    }

    @Test
    fun `overdue is strictly before today — today is not late`() {
        assertTrue(DueDates.isOverdue("2026-08-04", today))
        assertFalse(DueDates.isOverdue("2026-08-05", today))
        assertFalse(DueDates.isOverdue("2026-08-06", today))
        assertFalse(DueDates.isOverdue(null, today))
    }

    @Test
    fun `the week starts today and runs across a month boundary`() {
        val week = DueDates.week(LocalDate.of(2026, 8, 29))
        assertEquals(7, week.size)
        assertEquals(LocalDate.of(2026, 8, 29), week.first())
        assertEquals(LocalDate.of(2026, 9, 4), week.last())
    }

    @Test
    fun `weekday abbreviations are German`() {
        assertEquals("Mi", DueDates.weekdayShort(today))
        assertEquals("So", DueDates.weekdayShort(LocalDate.of(2026, 8, 9)))
    }
}
