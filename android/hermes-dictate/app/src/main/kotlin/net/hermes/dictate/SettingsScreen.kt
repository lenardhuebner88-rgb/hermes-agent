package net.hermes.dictate

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import net.hermes.dictate.ui.DictateMetrics
import net.hermes.dictate.ui.LocalDictate

/**
 * The full settings screen, structured as cards instead of the old 429-line flat
 * [LinearLayout] scroll. See `SettingsActivity` for what wires this to [DictatePrefs] and
 * Android services — this file only renders state and reports intents.
 */
@Composable
fun SettingsScreen(
    readiness: ReadinessState,
    onMicClick: () -> Unit,
    onEnableClick: () -> Unit,
    onSelectClick: () -> Unit,
    onOverlayClick: () -> Unit,
    form: SettingsFormState,
    onDictionaryChange: (String) -> Unit,
    onSnippetChange: (String) -> Unit,
    loginStatus: LoginStatus,
    onLoginClick: () -> Unit,
    onHermesHandoffClick: () -> Unit,
) {
    val deck = LocalDictate.current
    Column(
        Modifier
            .fillMaxWidth()
            .background(deck.background)
            .verticalScroll(rememberScrollState())
            .padding(DictateMetrics.screenPadding),
    ) {
        Text(
            stringResource(R.string.app_name),
            color = deck.textPrimary,
            fontSize = 26.sp,
            fontWeight = FontWeight.Bold,
        )
        Text(
            stringResource(R.string.settings_subtitle),
            color = deck.textDim,
            fontSize = 14.sp,
            modifier = Modifier.padding(top = 4.dp),
        )

        Spacer(Modifier.height(DictateMetrics.sectionGap))
        ReadinessCard(readiness, onMicClick, onEnableClick, onSelectClick, onOverlayClick)

        Spacer(Modifier.height(DictateMetrics.sectionGap))
        SectionCard(stringResource(R.string.section_overlay)) {
            Text(stringResource(R.string.overlay_hint), color = deck.textDim, fontSize = 13.sp)
            Spacer(Modifier.height(DictateMetrics.rowGap))
            SwitchRow(stringResource(R.string.cloud_preferred_switch), form.cloudPreferred) { form.cloudPreferred = it }
            SwitchRow(stringResource(R.string.flow_polish_switch), form.flowPolish) { form.flowPolish = it }
            SwitchRow(stringResource(R.string.local_refine_switch), form.localRefine) { form.localRefine = it }
            SliderRow(stringResource(R.string.bubble_size), form.bubbleSize.toFloat(), 70f..115f) {
                form.bubbleSize = it.toInt()
            }
            SliderRow(stringResource(R.string.bubble_opacity), form.bubbleOpacity.toFloat(), 20f..100f) {
                form.bubbleOpacity = it.toInt()
            }
            SwitchRow(stringResource(R.string.bubble_shrink), form.bubbleShrinkIdle) { form.bubbleShrinkIdle = it }
            SwitchRow(stringResource(R.string.local_recovery), form.localRecoveryEnabled) {
                form.localRecoveryEnabled = it
            }
            Spacer(Modifier.height(DictateMetrics.rowGap))
            OutlinedButton(onClick = onHermesHandoffClick, modifier = Modifier.fillMaxWidth()) {
                Text(stringResource(R.string.hermes_handoff_last))
            }
        }

        Spacer(Modifier.height(DictateMetrics.sectionGap))
        SectionCard(stringResource(R.string.section_dictionary)) {
            OutlinedTextField(
                value = form.dictionaryRules,
                onValueChange = onDictionaryChange,
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text(stringResource(R.string.dictionary_hint), color = deck.textDim) },
                minLines = 3,
            )
            Text(
                stringResource(R.string.dictionary_help),
                color = deck.textDim,
                fontSize = 12.sp,
                modifier = Modifier.padding(top = 4.dp),
            )
        }

        Spacer(Modifier.height(DictateMetrics.sectionGap))
        SectionCard(stringResource(R.string.section_snippets)) {
            OutlinedTextField(
                value = form.snippetRules,
                onValueChange = onSnippetChange,
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text(stringResource(R.string.snippet_hint), color = deck.textDim) },
                minLines = 3,
            )
            Text(
                stringResource(R.string.snippet_help),
                color = deck.textDim,
                fontSize = 12.sp,
                modifier = Modifier.padding(top = 4.dp),
            )
        }

        Spacer(Modifier.height(DictateMetrics.sectionGap))
        SectionCard(stringResource(R.string.section_style)) {
            ChoiceChips(
                options = listOf(
                    "auto" to stringResource(R.string.style_auto),
                    "neutral" to stringResource(R.string.style_neutral),
                    "formal" to stringResource(R.string.style_formal),
                    "casual" to stringResource(R.string.style_casual),
                    "concise" to stringResource(R.string.style_concise),
                ),
                selected = form.styleOverride,
                onSelect = { form.styleOverride = it },
            )
        }

        Spacer(Modifier.height(DictateMetrics.sectionGap))
        SectionCard(stringResource(R.string.section_language)) {
            ChoiceChips(
                options = listOf(
                    LanguageMode.SYSTEM.name to stringResource(R.string.lang_system),
                    LanguageMode.GERMAN.name to stringResource(R.string.lang_de),
                    LanguageMode.ENGLISH.name to stringResource(R.string.lang_en),
                    LanguageMode.AUTO.name to stringResource(R.string.lang_auto),
                ),
                selected = form.languageMode.name,
                onSelect = { form.languageMode = LanguageMode.valueOf(it) },
            )
        }

        Spacer(Modifier.height(DictateMetrics.sectionGap))
        SectionCard(stringResource(R.string.section_cloud)) {
            SwitchRow(stringResource(R.string.cloud_switch), form.cloudEnabled) { form.cloudEnabled = it }
            Text(
                stringResource(R.string.cloud_hint, DictateConfig.ALLOWED_HOST),
                color = deck.textDim,
                fontSize = 13.sp,
                modifier = Modifier.padding(top = 4.dp),
            )
            if (form.cloudEnabled) {
                Spacer(Modifier.height(DictateMetrics.rowGap))
                LoginRow(loginStatus, onLoginClick)
            }
        }

        Spacer(Modifier.height(DictateMetrics.sectionGap))
        Text(
            stringResource(R.string.privacy_note),
            color = deck.textDim,
            fontSize = 12.sp,
            modifier = Modifier.padding(bottom = 8.dp),
        )
    }
}

@Composable
private fun ReadinessCard(
    readiness: ReadinessState,
    onMicClick: () -> Unit,
    onEnableClick: () -> Unit,
    onSelectClick: () -> Unit,
    onOverlayClick: () -> Unit,
) {
    val deck = LocalDictate.current
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(DictateMetrics.cardRadius),
        colors = CardDefaults.cardColors(containerColor = deck.surface),
    ) {
        Column(Modifier.padding(DictateMetrics.cardPadding)) {
            Text(
                stringResource(R.string.section_setup),
                color = deck.accent,
                fontSize = 13.sp,
                fontWeight = FontWeight.Bold,
            )
            Spacer(Modifier.height(8.dp))
            if (readiness.allDone) {
                // Collapsed: the everyday user who is already set up should never have to
                // scroll past four rows of a task list that has nothing left to ask.
                Text(
                    stringResource(R.string.readiness_ready),
                    color = deck.success,
                    fontSize = 15.sp,
                    fontWeight = FontWeight.SemiBold,
                )
                return@Column
            }
            Text(
                stringResource(R.string.readiness_progress, readiness.doneCount, readiness.total),
                color = deck.textPrimary,
                fontSize = 15.sp,
                fontWeight = FontWeight.SemiBold,
            )
            Spacer(Modifier.height(DictateMetrics.rowGap))
            readiness.steps.forEach { step ->
                val (label, buttonLabel, onClick) = when (step.id) {
                    ReadinessStepId.MIC -> Triple(R.string.row_mic, R.string.btn_grant, onMicClick)
                    ReadinessStepId.ENABLE -> Triple(R.string.row_enable, R.string.btn_enable, onEnableClick)
                    ReadinessStepId.SELECT -> Triple(R.string.row_select, R.string.btn_select, onSelectClick)
                    ReadinessStepId.OVERLAY -> Triple(R.string.row_overlay, R.string.btn_overlay_enable, onOverlayClick)
                }
                ReadinessStepRow(stringResource(label), step.done, stringResource(buttonLabel), onClick)
            }
        }
    }
}

@Composable
private fun ReadinessStepRow(label: String, done: Boolean, buttonLabel: String, onClick: () -> Unit) {
    val deck = LocalDictate.current
    Row(
        Modifier.fillMaxWidth().padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, color = deck.textPrimary, fontSize = 14.sp, modifier = Modifier.weight(1f))
        if (done) {
            Text("✓", color = deck.success, fontSize = 14.sp, fontWeight = FontWeight.Bold)
        } else {
            Button(onClick = onClick, colors = ButtonDefaults.buttonColors(containerColor = deck.accent)) {
                Text(buttonLabel)
            }
        }
    }
}

@Composable
private fun SectionCard(title: String, content: @Composable ColumnScope.() -> Unit) {
    val deck = LocalDictate.current
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(DictateMetrics.cardRadius),
        colors = CardDefaults.cardColors(containerColor = deck.surface),
    ) {
        Column(Modifier.padding(DictateMetrics.cardPadding)) {
            Text(title, color = deck.accent, fontSize = 13.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(DictateMetrics.rowGap))
            content()
        }
    }
}

@Composable
private fun SwitchRow(label: String, checked: Boolean, onCheckedChange: (Boolean) -> Unit) {
    val deck = LocalDictate.current
    Row(
        Modifier.fillMaxWidth().padding(vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, color = deck.textPrimary, fontSize = 15.sp, modifier = Modifier.weight(1f))
        Switch(
            checked = checked,
            onCheckedChange = onCheckedChange,
            colors = SwitchDefaults.colors(checkedTrackColor = deck.accent),
        )
    }
}

@Composable
private fun SliderRow(label: String, value: Float, range: ClosedFloatingPointRange<Float>, onChange: (Float) -> Unit) {
    val deck = LocalDictate.current
    Column(Modifier.padding(vertical = 4.dp)) {
        Text(label, color = deck.textPrimary, fontSize = 14.sp)
        Slider(
            value = value,
            onValueChange = onChange,
            valueRange = range,
            colors = SliderDefaults.colors(thumbColor = deck.accent, activeTrackColor = deck.accent),
        )
    }
}

/**
 * Wraps instead of clipping — this is the direct fix for defect 1 from the audit: a
 * `RadioGroup` with `orientation="horizontal"` pushed the fifth "Knapp" option out of the
 * visible row. [FlowRow] structurally cannot do that; every chip that does not fit starts a
 * new line instead of disappearing off the edge.
 */
@Composable
private fun ChoiceChips(options: List<Pair<String, String>>, selected: String, onSelect: (String) -> Unit) {
    val deck = LocalDictate.current
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        options.forEach { (value, label) ->
            FilterChip(
                selected = value == selected,
                onClick = { onSelect(value) },
                label = { Text(label) },
                colors = FilterChipDefaults.filterChipColors(
                    selectedContainerColor = deck.accent,
                    selectedLabelColor = deck.background,
                ),
            )
        }
    }
}

@Composable
private fun LoginRow(status: LoginStatus, onLoginClick: () -> Unit) {
    val deck = LocalDictate.current
    Row(
        Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        val (text, color) = when (status) {
            LoginStatus.Checking -> stringResource(R.string.login_state_checking) to deck.textDim
            LoginStatus.SignedIn -> stringResource(R.string.login_state_in) to deck.success
            LoginStatus.SignedOut -> stringResource(R.string.login_state_out) to deck.error
            LoginStatus.Unreachable -> stringResource(R.string.login_state_unreachable) to deck.error
        }
        Text(stringResource(R.string.row_login), color = deck.textPrimary, fontSize = 15.sp, modifier = Modifier.weight(1f))
        Text(text, color = color, fontSize = 13.sp)
        Spacer(Modifier.width(12.dp))
        if (status.showsPrimaryLoginButton()) {
            Button(onClick = onLoginClick, colors = ButtonDefaults.buttonColors(containerColor = deck.accent)) {
                Text(stringResource(R.string.btn_login))
            }
        } else {
            OutlinedButton(onClick = onLoginClick) {
                Text(stringResource(R.string.btn_login_again))
            }
        }
    }
}
