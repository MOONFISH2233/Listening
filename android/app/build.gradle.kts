import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// DeepSeek API Key 从 local.properties 读，不写进代码。
// 本地开发：在 android/local.properties 里写 deepseek.api.key=sk-xxx
// CI：由 workflow 从 GitHub secret 写入 local.properties
val localProps = Properties().apply {
    val f = rootProject.file("local.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
val deepseekKey: String = localProps.getProperty("deepseek.api.key") ?: ""

android {
    namespace = "com.moonfish.listening"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.moonfish.listening"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "0.1"

        // 只出 arm64-v8a：模型 189 MB，多 ABI 会让 APK 翻几倍。
        // 2020 年后的手机基本都是 arm64。
        ndk {
            abiFilters += "arm64-v8a"
        }

        buildConfigField("String", "DEEPSEEK_API_KEY", "\"$deepseekKey\"")
    }

    buildFeatures {
        buildConfig = true
        viewBinding = true
    }

    // 模型是 .onnx，已经被 aapt 默认压缩过一轮；再压一次解压会变慢。
    // 这里关掉压缩，让 assets 原样打包。
    androidResources {
        noCompress += "onnx"
    }

    packaging {
        jniLibs {
            useLegacyPackaging = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.lifecycle:lifecycle-service:2.8.7")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    // DeepSeek 走 OpenAI 兼容 HTTP，用 OkHttp 直接发请求
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // 端侧流式识别。sherpa-onnx 把 Kotlin API 和 native so 打在一个 AAR 里，
    // 只发布在 JitPack，坐标的 groupId 是 com.github.k2-fsa（对应
    // settings.gradle.kts 里加的那个 jitpack 仓库）。
    // 官方示例工程 android/SherpaOnnx 用的就是这一条。
    implementation("com.github.k2-fsa:sherpa-onnx:v1.13.8")
}
