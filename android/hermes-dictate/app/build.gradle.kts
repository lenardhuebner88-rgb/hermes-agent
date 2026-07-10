plugins {
    id("com.android.application")
}

android {
    namespace = "net.hermes.dictate"
    compileSdk = 37

    defaultConfig {
        applicationId = "net.hermes.dictate"
        minSdk = 29
        targetSdk = 37
        versionCode = 1
        versionName = "1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
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
}

dependencies {
    implementation("androidx.core:core-ktx:1.19.0")
    implementation("androidx.activity:activity-ktx:1.13.0")

    testImplementation("junit:junit:4.13.2")
    // Host-JVM unit tests run against the android.jar org.json *stub* (throws "Stub!"); this
    // pulls in a real implementation of the same API for the test classpath only — production
    // code still only ever sees the platform's built-in org.json.
    testImplementation("org.json:json:20260522")
}
