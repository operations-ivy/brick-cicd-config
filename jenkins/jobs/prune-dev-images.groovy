// Delete dev-* image tags older than 3 days, from Docker Hub and the nodes.
// Nightly; they're only for trying a branch, and a rebuild is one push away.
// cron TZ=America/New_York
// cron H 5 * * *
pipeline {
    agent { label 'built-in' }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 30, unit: 'MINUTES') }
    stages {
        stage('Prune') {
            steps {
                script {
                    def status = sh(script: 'APPLY=true DEV_ONLY=true /usr/share/brick-jenkins/bin/prune-images', returnStatus: true)
                    if (status == 2) {
                        unstable('No Docker Hub credentials: Hub tags were only listed')
                    } else if (status != 0) {
                        error("prune-images exited ${status}")
                    }
                }
            }
        }
    }
}
