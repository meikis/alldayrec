plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

// CI explicitly selects a signer; ordinary local builds retain the existing debug-key behavior.
val ciSigningMode = System.getenv("LR_ANDROID_SIGNING_MODE")
fun ciSigningValue(name: String): String =
    System.getenv(name)?.takeIf { it.isNotBlank() }
        ?: error("Explicit CI signing requires $name")
if (ciSigningMode != null) {
    require(ciSigningMode in listOf("test", "release")) { "Unknown CI signing mode" }
}

android {
    namespace = "com.suyiiyii.live_record"
    compileSdk = 37
    compileSdkMinor = 0  // the SDK repository ships 37 as "android-37.0" only; plain 37 asks for "android-37"
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = "com.suyiiyii.live_record"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = 29
        targetSdk = flutter.targetSdkVersion
        // Uses the version code from pubspec.yaml. When using split APKs, 1000 * ABI_VERSION
        // is added automatically by Flutter. (https://developer.android.com/studio/build/configure-apk-splits#configure-APK-versions)
        // You can force using the value of versionCode by specifying the `-P force-version-code-ignoring-abi=true`
        // flag during build.
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        // The installed app was first signed with another Mac's debug key; an update must carry the same key or it
        // cannot install over it (and an uninstall drops the pieces not yet sent). That key lives here, outside git.
        getByName("debug") {
            val ks = file(System.getProperty("user.home") + "/.android/live-record-debug.keystore")
            if (ks.exists()) storeFile = ks
        }
        if (ciSigningMode != null) {
            create("ci") {
                storeFile = file(ciSigningValue("LR_ANDROID_KEYSTORE_PATH"))
                storePassword = ciSigningValue("ANDROID_KEYSTORE_PASSWORD")
                keyAlias = ciSigningValue("ANDROID_KEY_ALIAS")
                keyPassword = ciSigningValue("ANDROID_KEY_PASSWORD")
            }
        }
    }

    testOptions {
        unitTests.isReturnDefaultValues = true  // android.util.Log in the code the JVM tests drive
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.getByName(if (ciSigningMode != null) "ci" else "debug")
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}

android.sourceSets.getByName("test").resources.srcDir("../../../fixtures")

dependencies {
    implementation("no.nordicsemi.android:dfu:2.5.0")  // firmware updates over Bluetooth (legacy DFU, Adafruit bootloader)
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
