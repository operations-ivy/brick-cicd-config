// Run the seed again once Jenkins has loaded its jobs from disk.
//
// Configuration as code runs seed.groovy (values.yaml's jobs:) while Jenkins
// is still loading jobs from disk, and the load replaces what the seed just
// made: new jobs went missing until a restart (2026-10-13), and a new cron
// job's timer wasn't armed (2026-11-01). Init hooks like this one run after
// that load, so running the same seed here gets every job in on the first
// start. Job DSL is idempotent: the early run and this one agree, and the
// early one stays so a failure here can never leave Jenkins with no jobs.
//
// The image puts this in $JENKINS_HOME/init.groovy.d (as .override, so a new
// image's copy replaces the old one).
import javaposse.jobdsl.dsl.DslScriptLoader
import javaposse.jobdsl.plugin.JenkinsJobManagement

def seed = new File('/usr/share/brick-jenkins/seed.groovy')
try {
    def management = new JenkinsJobManagement(System.out, [:], new File('/usr/share/brick-jenkins'))
    new DslScriptLoader(management).runScript(seed.text)
    println "seed-after-load: ran ${seed}"
} catch (Exception e) {
    println "seed-after-load: failed, the early seed's jobs stay: ${e}"
}
