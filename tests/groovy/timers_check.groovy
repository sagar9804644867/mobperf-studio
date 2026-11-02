// Runs the EXACT Groovy timer scripts from a generated .jmx with JMeter-style bindings
// (Parameters, args, props, vars, log) and parses the result the way JMeter's JSR223Timer does (Long.parseLong).
//   groovy tests/groovy/timers_check.groovy path/to/script.jmx
import groovy.xml.XmlSlurper

def jmx = new XmlSlurper().parse(new File(args[0]))
def timers = jmx.'**'.findAll { it.name() == 'JSR223Timer' }
def prop = { t, String n -> t.stringProp.find { it.@name == n }.text() }
def byMarker = { String m -> timers.find { prop(it, 'script').contains(m) } }

class Vars { Map m = [:]; void putObject(String k, Object v) { m[k] = v }; Object getObject(String k) { m[k] } }
class Log { List w = []; void warn(String s) { w << s } }
int fails = 0
def check = { boolean c, String msg -> println((c ? "  PASS  " : "  FAIL  ") + msg); if (!c) fails++ }
def run = { String src, String p, Map props, Vars v = new Vars(), Log l = new Log() ->
    def pr = new Properties(); props.each { k, val -> pr.setProperty(k, val) }
    def o = new GroovyShell(new Binding([Parameters: p, args: p.split(' '), props: pr, vars: v, log: l])).evaluate(src)
    Long.parseLong(o.toString())
}

println "JSR223 timers in JMX: ${timers.size()}"
def tt = byMarker('mobperf:think')
def think = prop(tt, 'script'); def p = prop(tt, 'parameters')
def parts = p.trim().split(/\s+/)
long rec = parts[0] as long; int dev = parts.length > 1 ? (parts[1] as int) : 20
long d = (rec * dev).intdiv(100)
def xs = (1..300).collect { run(think, p, [:]) }
check(xs.every { it >= rec - d && it <= rec + d }, "baked default ${rec} ms +/-${dev}% -> ${xs.min()}..${xs.max()}")
if (d > 5) check(xs.unique().size() > 5, "values are random (${xs.unique().size()} distinct)")
xs = (1..200).collect { run(think, p, [think_ms: '1000', think_dev_pct: '20']) }
check(xs.every { it >= 800 && it <= 1200 }, "-Jthink_ms=1000 -Jthink_dev_pct=20 -> ${xs.min()}..${xs.max()}")
check(run(think, p, [think_dev_pct: '0']) == rec, "-Jthink_dev_pct=0 -> exactly ${rec}")
check(run(think, p, [think_ms: '0']) == 0, "-Jthink_ms=0 -> 0")
check(run(think, p, [think_ms: ' ']) >= 0, "blank -Jthink_ms -> baked value")
xs = (1..200).collect { run(think, p, [think_ms: '1000', think_dev_pct: '500']) }
check(xs.every { it >= 0 && it <= 2000 }, "variation over 100% is clamped -> ${xs.min()}..${xs.max()}")
check(run(think, '0', [:]) == 0, "Parameters '0' (old style, no %) -> 0")

def ps = prop(byMarker('mobperf:pacing_start'), 'script')
def pt = byMarker('mobperf:pacing_end'); def pe = prop(pt, 'script'); def pp = prop(pt, 'parameters')
def v = new Vars()
check(run(ps, '', [:], v) == 0 && v.getObject('mobperf_iteration_start') instanceof Long, "pacing start stores the time, waits 0")
check(run(pe, '0', [:], v) == 0, "pacing default 0 -> no wait")
v.putObject('mobperf_iteration_start', System.currentTimeMillis() - 1500)
long w = run(pe, '0', [pacing_ms: '5000'], v)
check(w >= 3400 && w <= 3500, "-Jpacing_ms=5000, iteration took 1500 -> waits ${w}")
v.putObject('mobperf_iteration_start', System.currentTimeMillis() - 1000)
w = run(pe, '4000', [:], v)
check(w >= 2900 && w <= 3000, "baked pacing 4000, iteration took 1000 -> waits ${w}")
check(run(pe, '4000', [pacing_ms: '0'], v) == 0, "-Jpacing_ms=0 turns baked pacing off")
def lg = new Log(); v.putObject('mobperf_iteration_start', System.currentTimeMillis() - 7000)
check(run(pe, '0', [pacing_ms: '5000'], v, lg) == 0 && lg.w.size() == 1, "slower than pacing -> 0 + warning")
check(run(pe, pp, [pacing_ms: '5000'], new Vars()) == 0, "no start time -> 0, no error")
println(fails ? "GROOVY: FAILURES ${fails}" : "GROOVY: ALL PASSED")
System.exit(fails ? 1 : 0)
