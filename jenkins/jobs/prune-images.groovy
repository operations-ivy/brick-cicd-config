// Keep only the newest two versions of each app image on Docker Hub, the nodes
// and brick9000 (nightly). Deletes; APPLY=false only lists what would go.
// cron TZ=America/New_York
// cron H(30-59) 5 * * *
// param APPLY=true Delete for real (false: only list what would be deleted)
pipeline {
    agent { label 'built-in' }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 30, unit: 'MINUTES') }
    stages {
        stage('Prune') {
            steps {
                script {
                    // 2: pruned everything but Docker Hub, for lack of credentials.
                    def status = sh(script: '/usr/share/brick-jenkins/bin/prune-images', returnStatus: true)
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
