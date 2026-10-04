package com.moonfish.listening

import android.content.Context
import android.util.Log
import com.k2fsa.sherpa.onnx.EndpointConfig
import com.k2fsa.sherpa.onnx.EndpointRule
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.OnlineModelConfig
import com.k2fsa.sherpa.onnx.OnlineRecognizer
import com.k2fsa.sherpa.onnx.OnlineRecognizerConfig
import com.k2fsa.sherpa.onnx.OnlineStream
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig
import java.io.File

/**
 * 端侧流式识别（sherpa-onnx + int8 zipformer 中英双语）。
 *
 * ⚠️ 关于准确率，先把话说在前面：
 *
 * 仓库里 spike-asr/RESULTS_real_audio.md 已经实测过，**这个模型在你现有的
 * 课堂录音上术语命中率 0/19**，输出是「跟知道离去中哥哥很远的那个跟」
 * 这类音节堆砌。spike-enhance 也验过降噪和高频补偿都救不回来。
 *
 * 所以这一版转写不准**不是代码写错了**，是模型能力 + 录音条件的已知限制。
 * 这个原型要验的是工程链路（能不能录满、能不能实时上屏、标记能不能触发 AI），
 * 不是准确率。换更强的模型是后面的事。
 *
 * ----
 *
 * 关于模型文件：sherpa-onnx 的 native 层要的是**文件路径**，不能直接读
 * assets 里的流。所以首次启动时把 assets 里的模型解压到 filesDir，
 * 之后每次都从那里加载。200 MB 解压一次大约几秒，用 .done 标记文件
 * 记录已完成，不重复解压。
 *
 * 关于 API：下面用的类名/字段名对齐的是 sherpa-onnx 的 Kotlin API，
 * 已对照官方 Android 示例工程（android/SherpaOnnx）核实过：
 * OnlineRecognizer / OnlineStream / OnlineRecognizerConfig /
 * OnlineTransducerModelConfig / FeatureConfig 都是真实存在的类，
 * decodingMethod、enableEndpoint、endpointConfig 等字段也对得上
 * （端点规则是嵌套的 EndpointRule，不是平铺参数——最初写错了，
 *   编译报 No parameter with name 'rule1MinTrailingSilence' 才发现）。
 * 依赖走 JitPack 的 com.github.k2-fsa:sherpa-onnx（见 app/build.gradle.kts）。
 *
 * 即便如此，所有和 sherpa-onnx 直接打交道的代码仍然只关在这一个文件里：
 * 换模型、换解码方式、真机上碰到签名不一致，都只改这里，不动别处。
 */
class AsrEngine(private val context: Context) {

    companion object {
        const val TAG = "AsrEngine"

        // assets 里的文件名。和 spike-asr/models/ 下的一致，
        // 这样 CI 直接从那个目录拷过来就行，不用改名。
        private const val ENCODER = "encoder-epoch-99-avg-1.int8.onnx"
        private const val DECODER = "decoder-epoch-99-avg-1.int8.onnx"
        private const val JOINER = "joiner-epoch-99-avg-1.int8.onnx"
        private const val TOKENS = "tokens.txt"

        private const val MODEL_SUBDIR = "models"

        /** 模型解压完成的标记。有它就跳过解压。 */
        private const val DONE_MARKER = ".extracted"

        /** 线程数。手机上是 4 大核居多，给 2 留点余量给录音和界面。 */
        private const val NUM_THREADS = 2
    }

    private var recognizer: OnlineRecognizer? = null
    private var stream: OnlineStream? = null

    /** 模型文件在手机上的落地目录。 */
    private fun modelDir(): File = File(context.filesDir, MODEL_SUBDIR)

    /**
     * 加载模型。会阻塞几秒（首次还要解压 200 MB）。
     *
     * 调用方应该在后台线程里调它——放在主线程会 ANR。
     */
    fun init() {
        if (recognizer != null) return

        val dir = ensureModelsExtracted()

        val config = OnlineRecognizerConfig(
            featConfig = FeatureConfig(
                sampleRate = AudioCapture.SAMPLE_RATE,
                featureDim = 80,
            ),
            modelConfig = OnlineModelConfig(
                transducer = OnlineTransducerModelConfig(
                    encoder = File(dir, ENCODER).absolutePath,
                    decoder = File(dir, DECODER).absolutePath,
                    joiner = File(dir, JOINER).absolutePath,
                ),
                tokens = File(dir, TOKENS).absolutePath,
                numThreads = NUM_THREADS,
                modelType = "zipformer",
                // 模型是 int8 量化过的，这里要显式告诉它，
                // 否则会按 fp32 解析，读出来的权重是错的。
                modelingUnit = "cjkchar",
            ),
            // greedy_search 是 spike-asr 里验证过的默认解码方式。
            // 那里也试过 modified_beam_search，**没有改善**，所以不折腾。
            decodingMethod = "greedy_search",
            enableEndpoint = true,
            // 端点规则不是平铺参数，而是包在 endpointConfig 里、每条一个
            // EndpointRule。签名照 v1.13.8 源码：
            //   EndpointRule(mustContainNonSilence, minTrailingSilence, minUtteranceLength)
            // 课堂上停顿多，判句结束比官方 demo 保守，断得太碎会缺上下文。
            endpointConfig = EndpointConfig(
                rule1 = EndpointRule(
                    mustContainNonSilence = false,
                    minTrailingSilence = 2.4f,
                    minUtteranceLength = 0.0f,
                ),
                rule2 = EndpointRule(
                    mustContainNonSilence = true,
                    minTrailingSilence = 1.2f,
                    minUtteranceLength = 0.0f,
                ),
                rule3 = EndpointRule(
                    mustContainNonSilence = false,
                    minTrailingSilence = 0.0f,
                    minUtteranceLength = 20.0f,
                ),
            ),
        )

        recognizer = OnlineRecognizer(assetManager = context.assets, config = config)
        stream = recognizer!!.createStream()
        Log.i(TAG, "识别引擎就绪，模型目录 ${dir.absolutePath}")
    }

    /**
     * 把 assets 里的模型解压到 filesDir。已经解压过就直接返回。
     *
     * 为什么不在每次启动时都解压：200 MB 拷贝一次要几秒，
     * 每次开 app 都等不划算。用 .extracted 标记记录状态。
     */
    private fun ensureModelsExtracted(): File {
        val dir = modelDir()
        val marker = File(dir, DONE_MARKER)

        if (marker.exists()) {
            // 标记在，但仍要确认文件真的都在（用户可能清过数据）
            val ok = listOf(ENCODER, DECODER, JOINER, TOKENS).all {
                File(dir, it).let { f -> f.exists() && f.length() > 0 }
            }
            if (ok) return dir
            Log.w(TAG, "标记在但模型文件不全，重新解压")
        }

        dir.mkdirs()
        Log.i(TAG, "开始解压模型到 ${dir.absolutePath}")

        for (name in listOf(ENCODER, DECODER, JOINER, TOKENS)) {
            val out = File(dir, name)
            context.assets.open(name).use { input ->
                out.outputStream().use { output ->
                    input.copyTo(output, bufferSize = 1 shl 16)
                }
            }
            Log.i(TAG, "解压完成 $name（${out.length() / 1024 / 1024} MB）")
        }

        marker.writeText("ok")
        return dir
    }

    /**
     * 喂一块 PCM 给识别器。
     *
     * PCM 是 Short，需要归一化成 [-1, 1] 的 Float——和官方 demo 一致
     * （spike-asr/run_asr.py 里也是 buffer / 32768.0）。
     */
    fun accept(samples: ShortArray, count: Int) {
        val r = recognizer ?: return
        val s = stream ?: return

        val f = FloatArray(count)
        for (i in 0 until count) {
            f[i] = samples[i] / 32768.0f
        }

        s.acceptWaveform(f, sampleRate = AudioCapture.SAMPLE_RATE)
        while (r.isReady(s)) {
            r.decode(s)
        }
    }

    /** 当前这句已识别出的文字（还没定稿）。 */
    fun currentText(): String {
        val r = recognizer ?: return ""
        val s = stream ?: return ""
        return try {
            r.getResult(s).text
        } catch (e: Exception) {
            Log.w(TAG, "取结果失败", e)
            ""
        }
    }

    /** 是否已经断句（可以定稿了）。 */
    fun isEndpoint(): Boolean {
        val r = recognizer ?: return false
        val s = stream ?: return false
        return try {
            r.isEndpoint(s)
        } catch (e: Exception) {
            false
        }
    }

    /** 断句后重置，开始下一句。 */
    fun reset() {
        val r = recognizer ?: return
        val s = stream ?: return
        try {
            r.reset(s)
        } catch (e: Exception) {
            Log.w(TAG, "reset 失败", e)
        }
    }

    /** 释放。可以重复调用。 */
    fun release() {
        try {
            stream?.release()
        } catch (e: Exception) {
            Log.w(TAG, "释放 stream 出错", e)
        }
        stream = null

        try {
            recognizer?.release()
        } catch (e: Exception) {
            Log.w(TAG, "释放 recognizer 出错", e)
        }
        recognizer = null
    }
}
