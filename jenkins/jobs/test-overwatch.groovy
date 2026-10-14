// Run overwatch's tests (poetry run pytest) in a throwaway pod on brick2000, on
// the same pinned Python image and Poetry as its Dockerfile. Also run nightly by test-all.
// param BRANCH=main Branch to test
pipeline {
    agent {
        kubernetes {
            defaultContainer 'python'
            // The pod's ServiceAccount has no token (values.yaml): tests can't touch the cluster.
            yaml '''
apiVersion: v1
kind: Pod
spec:
  containers:
    - name: python
      image: python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b
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
            // The agent's own container has git; the slim Python image doesn't.
            steps { script { plasma('checkout') { container('jnlp') { sh 'git clone --depth 1 --branch "$BRANCH" https://github.com/operations-ivy/overwatch src' } } } }
        }
        stage('Install') {
            steps { script { plasma('install') { dir('src') { sh 'pip install --no-cache-dir poetry==2.1.2 && poetry install --no-root --no-interaction' } } } }
        }
        stage('Test') {
            steps { script { plasma('test') { dir('src') { sh 'poetry run pytest -q' } } } }
        }
    }
}
