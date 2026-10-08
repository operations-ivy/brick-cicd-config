// Run the chucks-wisdom joke importer (a Kubernetes Job) with these settings.
// Leave QUERY empty to sample random jokes by category; set it to import the
// jokes matching a text search instead.
// param QUERY= Free-text search: import the jokes matching it (empty: random by category)
// param CATEGORIES= Comma-separated categories to sample (empty: all)
// param JOKES=1000 Stop after this many new jokes
// param TRIES_PER_CATEGORY=1000 Random pulls per category per pass
// param MAX_DUPLICATES=50 Duplicates before moving to the next category
// param SLEEP_SECONDS=60 Pause after each random pull, to go easy on the API
// param WAIT=false Wait for the import to finish (a random import takes hours)
pipeline {
    agent { label 'built-in' }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 24, unit: 'HOURS') }
    stages {
        stage('Import') {
            steps { sh '/usr/share/brick-jenkins/bin/chuck-importer' }
        }
    }
}
