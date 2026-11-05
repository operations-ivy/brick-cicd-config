// Keep maintenance windows tidy: delete the ones that have ended (readers already ignore them), and refresh maintenance-window's node list from Prometheus (every brick seen in the last 7 days, online or off, plus any in maintenance; never brick420), re-running the job seed when the list changed.
// cron H/5 * * * *
pipeline {
    agent { label 'built-in' }
    options { disableConcurrentBuilds(); timeout(time: 3, unit: 'MINUTES') }
    stages {
        stage('Refresh') {
            steps {
                script {
                    def rc = sh(script: '/usr/share/brick-jenkins/bin/maintenance refresh "$JENKINS_HOME/choices/maintenance-nodes"', returnStatus: true)
                    if (rc == 10) {
                        // the node list changed: rebuild the jobs so the dropdown shows it
                        jobDsl(scriptText: readFile('/usr/share/brick-jenkins/seed.groovy'), removedJobAction: 'IGNORE')
                    } else if (rc != 0) {
                        error("maintenance refresh failed (${rc})")
                    }
                }
            }
        }
    }
}
