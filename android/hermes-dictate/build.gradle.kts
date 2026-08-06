// Top-level build file; per-module configuration lives in app/build.gradle.kts
plugins {
    id("com.android.application") version "9.2.1" apply false
    // Must track AGP 9.2.1's bundled Kotlin (stdlib 2.4.0), not the newest
    // release — a mismatch fails as "incompatible metadata version". Version
    // measured in hermes-deck's build.gradle.kts, not guessed here.
    id("org.jetbrains.kotlin.plugin.compose") version "2.4.0" apply false
}
