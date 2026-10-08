// Keep local clones of brick-k8s-config, wigle-sync and chucks-wisdom current, so
// the other jobs work from them even when GitHub or the internet is down.
// cron H */2 * * *
pipeline {
    agent { label 'built-in' }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 15, unit: 'MINUTES') }
    stages {
        stage('Mirror') {
            steps { sh '/usr/share/brick-jenkins/bin/mirror-repos' }
        }
    }
}
