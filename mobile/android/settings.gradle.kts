pluginManagement {
    repositories {
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
        mavenCentral()
        gradlePluginPortal()
    }
    // Versions live here (not in the root build file) so that a machine without the Android SDK,
    // which only builds :core, never has to download the Android Gradle plugin.
    plugins {
        id("com.android.application") version "8.11.1"
        id("org.jetbrains.kotlin.android") version "2.2.0"
        id("org.jetbrains.kotlin.plugin.compose") version "2.2.0"
        id("org.jetbrains.kotlin.jvm") version "2.2.0"
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
        mavenCentral()
    }
}
rootProject.name = "Gusan"
include(":core")
// The app needs the Android SDK. Without one (a machine that only runs the logic tests) only :core is built.
val sdk = System.getenv("ANDROID_HOME") ?: System.getenv("ANDROID_SDK_ROOT")
val localSdk = file("local.properties").takeIf { it.exists() }?.readLines()?.any { it.startsWith("sdk.dir") } == true
if (sdk != null || localSdk) include(":app")
