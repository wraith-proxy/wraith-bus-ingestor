// Root project has no source of its own -- app/ is the only module. Plugins
// declared here with `apply false` are just version-pinned once, resolved
// via the exact versions in gradle.properties, and actually applied in
// app/build.gradle.kts.
plugins {
    id("com.android.application") version "8.13.2" apply false
    id("org.jetbrains.kotlin.android") version "2.2.0" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.2.0" apply false
}
