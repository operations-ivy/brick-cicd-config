// Run brick-cicd-config's tests (scripts/test) in a throwaway pod on brick2000.
// The pod runs this Jenkins image: it's already on brick2000, and has the git,
// Python 3 and ssh the tests need. Also run nightly by test-all.
// param BRANCH=main Branch to test
pipeline {
    agent {
        kubernetes {
            defaultContainer 'tests'
            // The tag must match controller.image.tag in values.yaml (a test checks).
            // The pod's ServiceAccount has no token (values.yaml): tests can't touch the cluster.
            yaml '''
apiVersion: v1
kind: Pod
spec:
  containers:
    - name: tests
      image: docker.io/whitepatrick/brick-jenkins:2.580.1-9
      command: [sleep]
      args: [infinity]
      resources:
        requests: {cpu: 250m, memory: 256Mi}
        limits: {cpu: "1", memory: 1Gi}
'''
        }
    }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 30, unit: 'MINUTES') }
    stages {
        stage('Checkout') {
            steps { script { plasma('checkout') { sh 'git clone --depth 1 --branch "$BRANCH" https://github.com/operations-ivy/brick-cicd-config src' } } }
        }
        stage('Test') {
            steps { script { plasma('test') { dir('src') { sh 'scripts/test' } } } }
        }
    }
}
