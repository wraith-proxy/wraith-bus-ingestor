import java.util.Properties

// local.properties is gitignored (see local.properties.example) -- reading
// it here, not hardcoding a topic or URL into a committed .kt file, is the
// whole point: whoever holds NTFY_TOPIC can read every alert this app
// raises, so it goes into BuildConfig the same way it goes into .env
// elsewhere in this repo.
val localProps = Properties().apply {
    val f = rootProject.file("local.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

fun localProp(key: String, default: String): String =
    (localProps.getProperty(key) ?: System.getenv(key) ?: default)

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "com.thomaspeoples.wraithbus"
    // Android 16 (API 36) end to end -- see docs/ROADMAP.md 1.5.0. This app
    // targets exactly one device (Tegu, a Pixel 9a phone running
    // LineageOS), so there is no reason to widen minSdk for compatibility
    // it will never need to exercise.
    compileSdk = 36

    defaultConfig {
        applicationId = "com.thomaspeoples.wraithbus"
        minSdk = 36
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"

        // Single-device scope (Tegu, a Pixel 9a phone) -- arm64-v8a is the
        // only architecture that will ever run this APK. Shipping the
        // other three (armeabi-v7a, x86, x86_64) MapLibre native slices
        // quadruples the native-lib payload for zero real devices.
        ndk {
            abiFilters += "arm64-v8a"
        }

        buildConfigField(
            "String", "API_BASE_URL",
            "\"${localProp("apiBaseUrl", "https://www.thomaspeoples.com/api/")}\""
        )
        buildConfigField(
            "String", "NTFY_TOPIC",
            "\"${localProp("ntfyTopic", "")}\""
        )
        buildConfigField(
            "String", "NTFY_WS_URL",
            "\"wss://www.thomaspeoples.com/ntfy/${localProp("ntfyTopic", "")}/ws\""
        )
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }
}

dependencies {
    // core-ktx 1.19.0 / compose 1.12.x / lifecycle 2.11.x all bumped their
    // minimum compileSdk to 37 in the same release wave -- 37 is still an
    // active preview cycle on this SDK (android-37.0/.1/.2, no plain stable
    // "37" yet), not the stable release docs/ROADMAP.md 1.5.0 actually
    // targets. One step back on each keeps compileSdk 36 (real, stable,
    // matches "Android 16+ minSdk") rather than chasing a preview platform
    // for a single-device app that ships once.
    implementation("androidx.core:core-ktx:1.18.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.10.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.10.0")
    implementation("androidx.activity:activity-compose:1.12.4")

    // Compose libraries pinned directly, not via a BOM -- one fewer moving
    // part, and the versions below are already a matched, verified set.
    implementation("androidx.compose.ui:ui:1.11.4")
    implementation("androidx.compose.ui:ui-graphics:1.11.4")
    implementation("androidx.compose.ui:ui-tooling-preview:1.11.4")
    implementation("androidx.compose.foundation:foundation:1.11.4")
    implementation("androidx.compose.runtime:runtime:1.11.4")
    implementation("androidx.compose.material3:material3:1.3.2")
    implementation("androidx.compose.material:material-icons-extended:1.7.8")
    // 2.10.1 transitively pulls lifecycle-*-compose 2.11.0, same
    // compileSdk-37 requirement as above -- one more step back.
    implementation("androidx.navigation:navigation-compose:2.9.8")
    debugImplementation("androidx.compose.ui:ui-tooling:1.11.4")

    // Networking + push, per docs/ROADMAP.md 1.5.0's explicit stack choice:
    // full native, Jetpack Compose + OkHttp, no Retrofit/Moshi -- org.json
    // (bundled in the Android platform) is enough for a handful of
    // hand-shaped endpoints, and OkHttp's own WebSocket client covers ntfy.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")

    // Same vector tile source and style the web map (ghost-site's
    // www/slaycoach/bus-map/) already renders -- see assets/style.json,
    // translated from that page's own MapLibre style object, not a second
    // cartography invented for this app.
    implementation("org.maplibre.gl:android-sdk:11.11.0")
}
