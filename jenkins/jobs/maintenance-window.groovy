// Put a brick in maintenance, or take it out: while it's in, brick9000's board and the Presto show it calm (no alerts, no caution colours) and nothing pages for it. The window ends by itself when DURATION is up; tick END to end it now. The node list is refreshed every 5 minutes by maintenance-refresh (every brick seen in the last 7 days, online or off, plus any in maintenance; never brick420).
// choice NODE=@maintenance-nodes The brick to put in (or take out of) maintenance
// choice DURATION=1h,4h,12h,24h,indefinite How long (indefinite: until END)
// param END=false Take NODE out of maintenance now (DURATION is ignored)
pipeline {
    agent { label 'built-in' }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 2, unit: 'MINUTES') }
    stages {
        stage('Maintenance') {
            steps {
                script {
                    currentBuild.displayName = params.END ? "#${currentBuild.number} ${params.NODE}: end" : "#${currentBuild.number} ${params.NODE}: ${params.DURATION}"
                }
                sh '/usr/share/brick-jenkins/bin/maintenance window'
            }
        }
    }
}
