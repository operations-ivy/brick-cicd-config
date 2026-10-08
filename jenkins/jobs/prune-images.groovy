// Keep only the newest two versions of each app image on Docker Hub, both nodes
// and brick9000. A dry run that lists what would go, unless APPLY is set.
// param APPLY=false Delete for real (otherwise only list what would be deleted)
pipeline {
    agent { label 'built-in' }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 30, unit: 'MINUTES') }
    stages {
        stage('Prune') {
            steps { sh '/usr/share/brick-jenkins/bin/prune-images' }
        }
    }
}
