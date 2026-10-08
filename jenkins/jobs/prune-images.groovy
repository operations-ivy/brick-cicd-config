// Keep only the newest two versions of each app image on Docker Hub, both nodes
// and brick9000. A dry run that lists what would go, unless APPLY is set.
// param APPLY=false Delete for real (otherwise only list what would be deleted)
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
