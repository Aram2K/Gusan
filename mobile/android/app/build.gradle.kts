import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "com.aram2k.gusan"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.aram2k.gusan"
        minSdk = 26
        targetSdk = 35
        versionCode = 2
        versionName = "0.2.0"
        // The Web OAuth client ID of the Google sign-in Gusan Cloud uses. With it, "Continue with Google"
        // opens the phone's own account sheet; without it, Google's page opens in an in-app browser tab.
        // Set it in gradle.properties (gusanGoogleWebClientId=...) or the GUSAN_GOOGLE_WEB_CLIENT_ID variable.
        val googleClient = (project.findProperty("gusanGoogleWebClientId") as String?)
            ?: System.getenv("GUSAN_GOOGLE_WEB_CLIENT_ID") ?: ""
        buildConfigField("String", "GOOGLE_WEB_CLIENT_ID", "\"$googleClient\"")
    }

    signingConfigs {
        // One debug key for every build (CI included), so its fingerprint can be registered with Google
        // once. Debug builds only: a release is signed with a key that is never in the repository.
        getByName("debug") {
            storeFile = file("debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    packaging {
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }

    lint {
        // The build must not fail on advice; real errors still stop it.
        abortOnError = true
        warningsAsErrors = false
        disable += setOf("GradleDependency", "OldTargetApi", "AndroidGradlePluginVersion")
    }
}

kotlin {
    compilerOptions { jvmTarget.set(JvmTarget.JVM_17) }
}

dependencies {
    implementation(project(":core"))
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.navigation.compose)
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.tooling.preview)
    implementation(libs.compose.material3)
    implementation(libs.compose.material.icons)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.androidx.credentials)
    implementation(libs.androidx.credentials.play.services)
    implementation(libs.googleid)
    implementation(libs.androidx.browser)
    debugImplementation(libs.compose.ui.tooling)
}
