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
