plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.habibi.remote"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.habibi.remote"
        minSdk = 26
        targetSdk = 34
        // Every GitHub build gets a higher version number, so Android treats it as an update.
        val run = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()
        versionCode = run
        versionName = "1.0.$run"
    }

    // Permanent signing key (signing/habibi-release.jks on the PC, a GitHub secret in CI).
    // Same key every build = phones can install updates over the old app.
    val keystorePath = System.getenv("HABIBI_KEYSTORE")
    val hasReleaseKey = keystorePath != null && file(keystorePath).exists()
    signingConfigs {
        create("habibi") {
            if (hasReleaseKey) {
                storeFile = file(keystorePath!!)
                storePassword = System.getenv("HABIBI_KEYSTORE_PASSWORD")
                keyAlias = "habibi"
                keyPassword = System.getenv("HABIBI_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (hasReleaseKey) signingConfigs.getByName("habibi")
                            else signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    lint {
        abortOnError = false
        checkReleaseBuilds = false
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
