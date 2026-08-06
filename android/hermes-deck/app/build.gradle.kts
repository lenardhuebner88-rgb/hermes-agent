plugins {
    // AGP 9 carries Kotlin itself, but still demands the Compose Compiler
    // Gradle plugin ("Starting in Kotlin 2.0, the Compose Compiler Gradle plugin
    // is required"). Its version has to track AGP's bundled Kotlin — measured,
    // not assumed: 2.2.10 failed as "metadata version 2.4.0, expected 2.2.0".
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {

    // One fixture, two test runners. `deck-pulse-live.json` is a real dashboard
    // response and the JVM tests already pin the parser against it; the
    // instrumented screenshot test renders the same bytes on a device. Copying
    // the file would let the two drift, and a screenshot of stale data is worse
    // than no screenshot.
    sourceSets.getByName("androidTest").resources.srcDir("src/test/resources")

    // The same bytes again, this time as debug *assets*, so `ShotActivity` can
    // render them on a device and be photographed from the host. Debug only —
    // the release APK never carries the fixture.
    sourceSets.getByName("debug").assets.srcDir("src/test/resources")

    namespace = "net.hermes.deck"
    compileSdk = 37

    defaultConfig {
        applicationId = "net.hermes.deck"
        minSdk = 29
        targetSdk = 37
        // The version had lived only in the APK's filename: six releases had
        // shipped while the app kept telling anyone who asked that it was 0.1.
        versionCode = 9
        versionName = "0.9"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildFeatures {
        compose = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    packaging {
        resources.excludes += setOf("/META-INF/{AL2.0,LGPL2.1}")
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2026.06.01")
    implementation(composeBom)

    implementation("androidx.core:core-ktx:1.19.0")
    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.11.0")
    // Pulled in transitively too, but the relay client and store depend on it
    // directly — an implicit version would be a silent break on any bump.
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.11.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.11.0")
    // Lifecycle-aware polling: the pulse loop must stop when the app leaves the
    // foreground, or a live screen keeps a journal query running in the pocket.
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.11.0")

    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-core")

    // Nostr transport: the relay speaks WebSocket, and Blossom uploads are raw
    // PUTs. OkHttp covers both; there is no platform WebSocket on Android.
    implementation("com.squareup.okhttp3:okhttp:5.4.0")

    // secp256k1 curve arithmetic. BIP-340 Schnorr itself is implemented in
    // Schnorr.kt because BouncyCastle ships no BIP-340 signer.
    implementation("org.bouncycastle:bcprov-jdk18on:1.85")

    implementation("io.coil-kt.coil3:coil-compose:3.5.0")
    implementation("io.coil-kt.coil3:coil-network-okhttp:3.5.0")

    // The private key never touches plain SharedPreferences.
    implementation("androidx.security:security-crypto:1.1.0")

    debugImplementation("androidx.compose.ui:ui-tooling")

    testImplementation("junit:junit:4.13.2")
    // The Android stub org.json throws "Stub!" on the host JVM test classpath;
    // the real implementation has to be on it. Same reason as hermes-voice.
    testImplementation("org.json:json:20260522")

    // Instrumented Compose tests. Until these existed the whole Compose layer
    // carried no gate at all — four of six defects in the 2026-08-05 session
    // lived exactly there and were found only by looking at a running emulator.
    // Versions read off Google's maven-metadata, not guessed.
    androidTestImplementation(composeBom)
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
    androidTestImplementation("androidx.test.ext:junit:1.3.0")
    androidTestImplementation("androidx.test:runner:1.7.0")
    // Supplies the empty activity `createComposeRule` launches into. Without it
    // every test dies with "No instrumentation registered".
    debugImplementation("androidx.compose.ui:ui-test-manifest")
}
