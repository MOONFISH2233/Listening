package com.moonfish.listening

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.util.concurrent.TimeUnit

/**
 * DeepSeek 客户端 —— 把「老师刚说的话」变成「学生能照着说的答案」。
 *
 * 走的是 OpenAI 兼容接口：
 *   POST https://api.deepseek.com/chat/completions
 *   Authorization: Bearer <key>
 *   body: { model, messages, stream }
 *
 * 为什么用流式（stream=true）：课堂上等一个 500 字的回答要好几秒，
 * 边生成边显示，学生第一秒就能开始读。
 *
 * ⚠️ 一个必须交代的现实问题：这一环的上游是端侧识别，而**端侧识别在
 * 现有素材上是错的**（见 AsrEngine 的注释和 RESULTS_real_audio.md）。
 * 转出来的文本常常是「跟知道离去中哥哥很远的那个跟」这种音节堆砌，
 * 喂给再强的模型也出不了正确答案。
 *
 * 所以这一环现在的价值是**验证链路通不通**，不是验证答案好不好。
 * 想让它真有用，前面那一步（识别）得先解决——要么换更强的模型，
 * 要么走云端 ASR。这不在本版范围内。
 *
 * 为了让链路可测，这里做了一个诚实的兜底：如果识别文本明显不成句，
 * 请求里会带上这一点，让模型自己判断「这段文字可能识别有误」，
 * 而不是硬编一个答案出来。
 */
class LlmClient(private val apiKey: String) {

    companion object {
        const val TAG = "LlmClient"
        private const val ENDPOINT = "https://api.deepseek.com/chat/completions"

        // 文档里给的可用模型名。deepseek-flash 是当前推荐的那个。
        private const val MODEL = "deepseek-flash"

        /** 上下文取多久的逐字稿。60 秒大概是一两段讲解。 */
        const val CONTEXT_SECONDS = 60.0
    }

    private val http = OkHttpClient.Builder()
        // 生成可能比较久，读超时给足；连接超时不用长。
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    val isConfigured: Boolean get() = apiKey.isNotBlank()

    /**
     * 就一段上下文提问，流式回调增量文本。
     *
     * @param userQuestion 用户想问的（自由提问时用）。为空则按标记类型自动生成问题。
     * @param onDelta 每收到一小段就回调一次。**在 IO 线程上调用**，界面要自己切主线程。
     * @return 完整回答；失败返回空串（错误通过 onDelta 之外的方式记日志）
     */
    suspend fun ask(
        context: String,
        markerType: String?,
        userQuestion: String?,
        onDelta: (String) -> Unit,
    ): String = withContext(Dispatchers.IO) {
        if (!isConfigured) {
            Log.w(TAG, "没有配置 DeepSeek API Key")
            return@withContext ""
        }
        if (context.isBlank()) {
            return@withContext ""
        }

        val question = userQuestion?.takeIf { it.isNotBlank() }
            ?: defaultQuestion(markerType)

        val body = buildBody(context, question)

        val request = Request.Builder()
            .url(ENDPOINT)
            .addHeader("Authorization", "Bearer $apiKey")
            .addHeader("Content-Type", "application/json")
            .post(body.toRequestBody("application/json".toMediaType()))
            .build()

        val full = StringBuilder()
        try {
            http.newCall(request).execute().use { resp ->
                if (!resp.isSuccessful) {
                    val err = resp.body?.string().orEmpty()
                    Log.e(TAG, "DeepSeek 返回 ${resp.code}：$err")
                    return@withContext ""
                }

                val reader: BufferedReader = resp.body!!.source().inputStream().bufferedReader()
                reader.useLines { lines ->
                    for (line in lines) {
                        if (!line.startsWith("data:")) continue
                        val payload = line.removePrefix("data:").trim()
                        if (payload.isEmpty() || payload == "[DONE]") continue

                        val delta = extractDelta(payload) ?: continue
                        if (delta.isNotEmpty()) {
                            full.append(delta)
                            onDelta(delta)
                        }
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "请求 DeepSeek 失败", e)
            return@withContext full.toString()
        }

        full.toString()
    }

    /** 按标记类型给一个默认问法。 */
    private fun defaultQuestion(markerType: String?): String = when (markerType) {
        Marker.TYPE_NOT_UNDERSTAND ->
            "老师刚才讲的这段我没听懂。请用一两句话把核心意思讲清楚，" +
                "再给一个能让我回答老师提问的说法。"
        Marker.TYPE_QUESTION ->
            "老师刚才可能提问了。请根据这段内容推测他在问什么，" +
                "并给一个我可以说出口的回答。"
        Marker.TYPE_IMPORTANT ->
            "把这段内容压缩成三句话的要点，方便我记笔记。"
        else ->
            "根据这段课堂内容，回答我的问题。"
    }

    private fun buildBody(context: String, question: String): String {
        val system = """
            你是课堂上的实时助教。学生把老师讲的话（语音转写）发给你，
            你要帮他快速理解并给出**可以直接说出口**的回答。

            要求：
            1. 说人话，短。学生要在几秒内看完并开口。
            2. 直接给结论和说法，不要「首先/其次/总之」这种套话。
            3. 如果转写文本明显不成句（语音识别出错），**如实说明可能识别有误**，
               并尽量从能辨认的片段里推断老师在讲什么。不要硬编一个答案。
            4. 用中文回答。
        """.trimIndent()

        val user = """
            【课堂转写片段】
            $context

            【学生想问】
            $question
        """.trimIndent()

        val messages = JSONArray()
            .put(JSONObject().put("role", "system").put("content", system))
            .put(JSONObject().put("role", "user").put("content", user))

        return JSONObject()
            .put("model", MODEL)
            .put("messages", messages)
            .put("stream", true)
            .put("temperature", 0.3)   // 要的是准确复述课堂内容，不是发挥
            .toString()
    }

    /**
     * 从一行 SSE 里取出增量文本。
     *
     * 兼容两种字段：DeepSeek 的推理模型会先吐 reasoning_content（思维链），
     * 正文在 content 里。**只取 content**——思维链不该显示给学生。
     */
    private fun extractDelta(payload: String): String? {
        return try {
            val obj = JSONObject(payload)
            val choices = obj.optJSONArray("choices") ?: return null
            if (choices.length() == 0) return null
            val delta = choices.getJSONObject(0).optJSONObject("delta") ?: return null
            delta.optString("content", "")
        } catch (e: Exception) {
            Log.w(TAG, "解析 SSE 行失败：$payload", e)
            null
        }
    }
}
