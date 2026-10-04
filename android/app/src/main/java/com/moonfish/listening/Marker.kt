package com.moonfish.listening

import android.content.Context
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 一次「标记」——学生上课时按下按钮的那个瞬间。
 *
 * 存下来的不只是时间点，还有**当时前后一段的转写文本**。
 * 为什么：出答案要的是上下文，只给一个时间戳，模型不知道老师在讲什么。
 * 所以按下按钮时，把最近一段逐字稿一起冻进这条记录里。
 *
 * 标记是纯本地数据，先落盘再发请求——网络失败也不该弄丢学生标过的东西。
 */
data class Marker(
    /** 类型：not_understand / question / important */
    val type: String,
    /** 按下时的录音时刻（秒） */
    val atSeconds: Double,
    /** 墙上时间，用于展示和排序 */
    val createdAtMs: Long,
    /** 按下时冻结的上下文文本 */
    val context: String,
    /** AI 的回答。还没问过就是空串 */
    val answer: String = "",
) {
    companion object {
        const val TYPE_NOT_UNDERSTAND = "not_understand"
        const val TYPE_QUESTION = "question"
        const val TYPE_IMPORTANT = "important"

        fun label(type: String): String = when (type) {
            TYPE_NOT_UNDERSTAND -> "没听懂"
            TYPE_QUESTION -> "有问题"
            TYPE_IMPORTANT -> "重点"
            else -> type
        }
    }

    fun toJson(): JSONObject = JSONObject().apply {
        put("type", type)
        put("atSeconds", atSeconds)
        put("createdAtMs", createdAtMs)
        put("context", context)
        put("answer", answer)
    }

    fun timeLabel(): String =
        SimpleDateFormat("HH:mm:ss", Locale.US).format(Date(createdAtMs))
}

/**
 * 标记的落盘与读取。
 *
 * 存成 JSON 数组，一次课一个文件，跟录音同名（xxx.markers.json）。
 * 这样把 WAV 拉到电脑时，标记也跟着走，不用另一套命名规则去对。
 */
object MarkerStore {

    const val TAG = "MarkerStore"

    fun fileFor(ctx: Context, recordingTitle: String): File =
        File(RecorderService.recordingsDir(ctx), "$recordingTitle.markers.json")

    fun save(ctx: Context, recordingTitle: String, markers: List<Marker>): Boolean {
        return try {
            val arr = JSONArray()
            markers.forEach { arr.put(it.toJson()) }
            fileFor(ctx, recordingTitle).writeText(arr.toString(2))
            true
        } catch (e: Exception) {
            Log.e(TAG, "保存标记失败", e)
            false
        }
    }

    fun load(ctx: Context, recordingTitle: String): MutableList<Marker> {
        val f = fileFor(ctx, recordingTitle)
        if (!f.exists()) return mutableListOf()

        return try {
            val arr = JSONArray(f.readText())
            MutableList(arr.length()) { i ->
                val o = arr.getJSONObject(i)
                Marker(
                    type = o.optString("type"),
                    atSeconds = o.optDouble("atSeconds", 0.0),
                    createdAtMs = o.optLong("createdAtMs", 0L),
                    context = o.optString("context"),
                    answer = o.optString("answer"),
                )
            }
        } catch (e: Exception) {
            Log.e(TAG, "读取标记失败", e)
            mutableListOf()
        }
    }
}
