plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.coomi.kimi"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.coomi.kimi"
        minSdk = 24
        // targetSdk 28 is deliberate, not laziness: Android 10+ blocks execve()
        // on files inside the app's data directory for apps targeting 29+. The
        // whole runtime (proot, the Ubuntu rootfs, the agent venv) lives there,
        // exactly like Termux, which pins 28 for the same reason. Do not bump it
        // without moving the runtime out of the data dir.
        targetSdk = 28
        versionCode = 1
        versionName = "0.1.0"
        ndk {
            // The payload is arm64-only: rootfs and Kimi Code binaries are.
            abiFilters += listOf("arm64-v8a")
        }
    }

    // Payload tarballs are already gzipped; re-compressing them wastes build time.
    androidResources {
        noCompress += listOf("gz", "tar")
    }

    packaging {
        // libproot.so / libkimi.so must exist on disk to be execve'd; an
        // uncompressed-in-APK (extractNativeLibs=false) library has no path.
        jniLibs.useLegacyPackaging = true
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
        }
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
        }
    }

    buildFeatures {
        // Off by default since AGP 8; AgentService reports the version to the guest.
        buildConfig = true
    }

    testOptions {
        unitTests {
            // The CI log only showed "FileNotFoundException at <call site>",
            // which is not enough to tell whether the reader or the filesystem
            // failed. Full traces + stream output make a red test self-explaining.
            all {
                it.testLogging {
                    events("failed", "skipped")
                    exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
                    showStandardStreams = true
                    showCauses = true
                    showStackTraces = true
                }
                it.outputs.upToDateWhen { false }
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    // assets/ holds rootfs.tar.gz, deps.tar.gz, app.tar.gz, kimi/kimi, bin/proot
    // produced by scripts/package_payload.sh. Missing files are a packaging
    // error, caught by CI's preflight step, not here.
    sourceSets {
        getByName("main") {
            assets.srcDirs("assets")
            jniLibs.srcDirs("jniLibs")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.webkit:webkit:1.11.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    testImplementation("junit:junit:4.13.2")
}
