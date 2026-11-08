// Every finished Jenkins build as one JSON line in Loki, so Grafana can mark
// changes on its graphs (brick-cicd-config#90, the cheap version: no Kafka).
//
//   {source="jenkins", kind="change"}
//   {"job": "deploy-brick9000", "build": 42, "result": "SUCCESS", "duration_s": 31,
//    "cause": "Generic Cause", "params": {"BRANCH": "main"}, "url": "job/deploy-brick9000/42/"}
//
// Loki runs with auth_enabled: false, so the push needs no token. A RunListener
// sees every build of every job from one place, so no job file changes. The
// two jobs that poll every few minutes are left out: their real work shows up
// as the builds they start. A push that fails is printed and dropped; it never
// fails the build.
//
// The image puts this in $JENKINS_HOME/init.groovy.d (as .override, so a new
// image's copy replaces the old one), next to seed-after-load.groovy.
import groovy.json.JsonOutput
import hudson.model.ParametersAction
import hudson.model.Run
import hudson.model.TaskListener
import hudson.model.listeners.RunListener

class BrickChanges extends RunListener<Run> {
    static final String LOKI = System.getenv('BRICK_LOKI_PUSH_URL') ?:
        'http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push'
    static final Set<String> SKIP = ['maintenance-refresh', 'build-branches'] as Set
    // The parameters that say what changed; the rest (help text, toggles) don't.
    static final Set<String> PARAMS = ['BRANCH', 'NODE', 'ACTION', 'DURATION', 'SHUTDOWN', 'DRY_RUN', 'END',
                                       'IMAGE', 'REF', 'TAG', 'APPLY'] as Set

    BrickChanges() { super(Run) }

    @Override
    void onCompleted(Run run, TaskListener listener) {
        String job = run.parent.fullName
        if (job in SKIP) {
            return
        }
        try {
            push(event(run, job), run.startTimeInMillis + run.duration)
        } catch (Exception e) {
            println "changes-listener: ${job} #${run.number} not logged: ${e}"
        }
    }

    static Map event(Run run, String job) {
        def params = [:]
        run.getAction(ParametersAction)?.parameters?.each { p ->
            if (p.name in PARAMS && p.value != null && p.value.toString() != '') {
                params[p.name] = p.value.toString()
            }
        }
        [job: job, build: run.number, result: run.result?.toString() ?: 'UNKNOWN',
         duration_s: (long) (run.duration / 1000), cause: run.causes*.shortDescription.join('; '),
         params: params, url: "job/${job}/${run.number}/".toString()]
    }

    static void push(Map event, long finishedMillis) {
        def body = JsonOutput.toJson([streams: [[
            stream: [source: 'jenkins', kind: 'change'],
            values: [["${finishedMillis}000000".toString(), JsonOutput.toJson(event)]],
        ]]])
        def connection = new URL(LOKI).openConnection()
        connection.requestMethod = 'POST'
        connection.doOutput = true
        connection.connectTimeout = 3000
        connection.readTimeout = 3000
        connection.setRequestProperty('Content-Type', 'application/json')
        connection.outputStream.withWriter('UTF-8') { it << body }
        int code = connection.responseCode
        if (code >= 300) {
            println "changes-listener: Loki answered ${code} for ${event.job} #${event.build}"
        }
    }
}

// Once per start: drop a copy left from an earlier run of this script, then add.
def listeners = RunListener.all()
listeners.removeAll(listeners.findAll { it.class.name == 'BrickChanges' })
listeners.add(new BrickChanges())
println 'changes-listener: every finished build goes to Loki'
