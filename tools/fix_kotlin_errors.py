endpointConfig = EndpointConfig(
    rule1 = EndpointRule(mustContainNonSilence = false, minTrailingSilence = 2.4f, minUtteranceLength = 0f),
    rule2 = EndpointRule(mustContainNonSilence = true,  minTrailingSilence = 1.2f, minUtteranceLength = 0f),
    rule3 = EndpointRule(mustContainNonSilence = false, minTrailingSilence = 0f,   minUtteranceLength = 20f),
)
```

三个文件我一起修（`AsrEngine.kt`、`MainActivity.kt`、`RecorderService.kt`），用 Python 脚本改，避开转义坑。


```python
"""修 CI 报的 6 个 Kotlin 编译错。

错误清单（来自 #10 的构建日志）：
  AsrEngine.kt:105-107   No parameter with name 'rule1MinTrailingSilence' found
  MainActivity.kt:338    Unresolved reference 'it'
  RecorderService.kt:204 Smart cast to 'AsrEngine' is impossible
  RecorderService.kt:208 Smart cast to 'AsrEngine' is impossible

修法：
  1. 端点参数属于 endpointConfig，且每条规则是 EndpointRule 对象。
     真实签名（从 v1.13.8 源码读到，不是猜的）：
       data class EndpointRule(mustContainNonSilence, minTrailingSilence, minUtteranceLength)
       data class EndpointConfig(rule1, rule2, rule3)
  2. View.post 的 lambda 是 Runnable，无参数，不该写 it。
  3. asr 是可空可变属性，判空后取局部 val，编译器才认智能转换。
"""
import pathlib

root = pathlib.Path(r"D:\创业\听刻\android\app\src\main\java\com\moonfish\listening")


def patch(name, old, new, note):
    p = root / name
    src = p.read_text(encoding="utf-8")
    if old not in src:
        print(f"!! {name}: 没匹配到 —— {note}")
        return False
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(src.replace(old, new, 1))
    print(f"OK {name}: {note}")
    return True


# ---- 1. AsrEngine.kt：端点配置 ----
patch(
    "AsrEngine.kt",
    """            decodingMethod = "greedy_search",
            enableEndpoint = true,
            // 端点规则：静音 2.4 秒判定一句结束。
            // 比官方 demo 的默认值保守一些——课堂上停顿多，
            // 断得太碎会让每句都缺上下文。
            rule1MinTrailingSilence = 2.4f,
            rule2MinTrailingSilence = 1.2f,
            rule3MinUtteranceLength = 20f,
        )""",
    """            decodingMethod = "greedy_search",
            enableEndpoint = true,
            // 端点规则。注意这三条不是平铺的参数，而是包在 endpointConfig 里、
            // 每条又是一个 EndpointRule —— 签名照 v1.13.8 的源码写：
            //   EndpointRule(mustContainNonSilence, minTrailingSilence, minUtteranceLength)
            //
            // 取值理由：课堂上停顿多，判句结束要比官方 demo 保守，
            // 断得太碎会让每句都缺上下文。rule1 管「有内容且静了 2.4 秒」，
            // rule2 管「哪怕没内容、静满 1.2 秒也断」，rule3 兜底「说满 20 秒必断」。
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
        )""",
    "端点参数改成 EndpointConfig/EndpointRule",
)

# 补 import
patch(
    "AsrEngine.kt",
    "import com.k2fsa.sherpa.onnx.FeatureConfig",
    "import com.k2fsa.sherpa.onnx.EndpointConfig\nimport com.k2fsa.sherpa.onnx.EndpointRule\nimport com.k2fsa.sherpa.onnx.FeatureConfig",
    "补 EndpointConfig / EndpointRule 的 import",
)

# 顺带修那句已经被编译器证伪的注释
patch(
    "AsrEngine.kt",
    " * decodingMethod、enableEndpoint、rule1MinTrailingSilence 等字段也对得上。",
    " * decodingMethod、enableEndpoint、endpointConfig 等字段也对得上\n * （端点规则是嵌套的 EndpointRule，不是平铺参数——这一点最初写错了，\n *   CI 编译报 No parameter with name 'rule1MinTrailingSilence' 才发现）。",
    "修掉被编译器证伪的注释",
)

# ---- 2. MainActivity.kt：post 的 lambda 无参数 ----
patch(
    "MainActivity.kt",
    """        findViewById<android.widget.ScrollView>(R.id.scrollTranscript)
            .post { it.fullScroll(android.view.View.FOCUS_DOWN) }""",
    """        val sv = findViewById<android.widget.ScrollView>(R.id.scrollTranscript)
        // View.post 接的是 Runnable，lambda 没有参数，所以这里不能写 it。
        sv.post { sv.fullScroll(android.view.View.FOCUS_DOWN) }""",
    "post 的 lambda 去掉 it",
)

# ---- 3. RecorderService.kt：智能转换 ----
patch(
    "RecorderService.kt",
    """            try {
                asr?.accept(buffer, n)
                val pending = asr?.currentText().orEmpty()
                if (asr?.isEndpoint() == true) {
                    val seg = asr.currentText()
                    if (seg.isNotBlank()) {
                        finalized.append(seg).append('\\n')
                    }
                    asr.reset()
                }
                notifyTranscript(pending)""",
    """            try {
                // 先取成局部 val：asr 是可空可变属性，直接连着调
                // 编译器无法做智能转换（可能被别的线程改掉）。
                val engine = asr
                if (engine != null) {
                    engine.accept(buffer, n)
                    val pending = engine.currentText()
                    if (engine.isEndpoint()) {
                        if (pending.isNotBlank()) {
                            finalized.append(pending).append('\\n')
                        }
                        engine.reset()
                    }
                    notifyTranscript(pending)
                }""",
    "asr 取局部 val，修智能转换",
)

print("\n完成。")
