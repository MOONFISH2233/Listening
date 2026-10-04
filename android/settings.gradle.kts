pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // sherpa-onnx 的 Android AAR 只发布在 JitPack 上（groupId 是
        // com.github.k2-fsa，不是 com.k2fsa）。官方示例工程用的就是这条。
        maven { url = uri("https://jitpack.io") }
    }
}

rootProject.name = "听刻"
include(":app")
```



```kotlin
// 顶层构建文件：只声明插件版本，不在这里配置模块。
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
}
```



```properties
# JVM 参数：Gradle 编译 200 MB 的 assets 时需要多一些堆
org.gradle.jvmargs=-Xmx4g -Dfile.encoding=UTF-8

# AndroidX（必须）
android.useAndroidX=true
android.enableJetifier=false

kotlin.code.style=official
```



```properties
distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https\://services.gradle.org/distributions/gradle-8.9-bin.zip
networkTimeout=10000
validateDistributionUrl=true
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
