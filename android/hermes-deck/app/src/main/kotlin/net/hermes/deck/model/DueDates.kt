package net.hermes.deck.model

import java.time.LocalDate
import java.time.format.DateTimeParseException

/**
 * Everything the deck knows about a due date.
 *
 * `DeckTask.due` is the raw string from the wire tag `["due", …]`, written by
 * this app and by anything else posting into the channel. So parsing has to
 * survive a value it did not write: an unparseable date is treated as "no
 * date", never as an exception in the middle of a list.
 *
 * Pure and free of Android, like [TaskCodec] and [TaskFilter], so the rules
 * below are covered by JVM tests.
 */
object DueDates {

    /** The one format this app writes. ISO, so it sorts as a string too. */
    fun format(date: LocalDate): String = date.toString()

    fun parse(due: String?): LocalDate? {
        val text = due?.trim().orEmpty()
        if (text.isEmpty()) return null
        return try {
            LocalDate.parse(text)
        } catch (_: DateTimeParseException) {
            null
        }
    }

    /** Days a task can be filed under, starting today. */
    fun week(today: LocalDate, days: Int = 7): List<LocalDate> =
        (0 until days).map { today.plusDays(it.toLong()) }

    fun isOverdue(due: String?, today: LocalDate): Boolean {
        val date = parse(due) ?: return false
        return date.isBefore(today)
    }

    /**
     * Short human label for a card. Relative where relative is clearer —
     * "Heute" says more at a glance than a date the reader has to compare.
     */
    fun label(due: String?, today: LocalDate): String? {
        val date = parse(due) ?: return null
        val delta = date.toEpochDay() - today.toEpochDay()
        return when {
            delta == 0L -> "Heute"
            delta == 1L -> "Morgen"
            delta == -1L -> "Gestern"
            delta in 2L..6L -> weekdayShort(date)
            else -> dayMonth(date)
        }
    }

    /** "Mo", "Di", … — German, two letters, for the day strip and labels. */
    fun weekdayShort(date: LocalDate): String = when (date.dayOfWeek.value) {
        1 -> "Mo"
        2 -> "Di"
        3 -> "Mi"
        4 -> "Do"
        5 -> "Fr"
        6 -> "Sa"
        else -> "So"
    }

    /** "12.08." — no year; a deck does not plan that far out. */
    fun dayMonth(date: LocalDate): String =
        "%02d.%02d.".format(date.dayOfMonth, date.monthValue)
}
