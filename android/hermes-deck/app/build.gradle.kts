plugins {
    // AGP 9 carries Kotlin itself, but still demands the Compose Compiler
    // Gradle plugin ("Starting in Kotlin 2.0, the Compose Compiler Gradle plugin
    // is required"). Its version has to track AGP's bundled Kotlin — measured,
    // not assumed: 2.2.10 failed as "metadata version 2.4.0, expected 2.2.0".
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "net.hermes.deck"
    compileSdk = 37

    defaultConfig {
        applicationId = "net.hermes.deck"
        minSdk = 29
        targetSdk = 37
        versionCode = 1
        versionName = "0.1"

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
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.11.0")

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
}
