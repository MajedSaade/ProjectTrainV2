def sendBuildEmail(String status) {
    withCredentials([
        string(credentialsId: 'EmailToSend', variable: 'NOTIFY_TO'),
        usernamePassword(
            credentialsId: 'smtp-credentials',
            usernameVariable: 'SMTP_USER',
            passwordVariable: 'SMTP_PASSWORD'
        )
    ]) {
        sh """
            docker run --rm -i \\
                -e NOTIFY_TO \\
                -e SMTP_USER \\
                -e SMTP_PASSWORD \\
                -e SMTP_HOST=\${SMTP_HOST} \\
                -e SMTP_PORT=\${SMTP_PORT} \\
                -e BUILD_STATUS=${status} \\
                -e JOB_NAME=\${JOB_NAME} \\
                -e BUILD_NUMBER=\${BUILD_NUMBER} \\
                -e BUILD_URL=\${BUILD_URL} \\
                -e DOCKER_IMAGE_NAME=\${DOCKER_IMAGE_NAME} \\
                python:3.11-slim python - < scripts/notify.py
        """
    }
}

pipeline {
    agent {
        label 'agent-1'
    }

    environment {
        DOCKER_REGISTRY_HOST = 'docker.io'
        DOCKER_IMAGE_NAME = 'majedsaade/telemetry-pipeline'
        DOCKER_CREDENTIALS_ID = 'dockerhub-registry-Credentials'

        COMPOSE_FILE = 'compose/docker-compose.deploy.yml'
        IMAGE_TAG = "${env.BUILD_NUMBER}"

        SMTP_HOST = 'smtp.gmail.com'
        SMTP_PORT = '587'
    }

    triggers {
        githubPush()
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Validate') {
            steps {
                sh '''
                    set -euo pipefail
                    chmod +x scripts/run_validation.sh
                    ./scripts/run_validation.sh
                '''
            }
        }

        stage('Build and Push') {
            steps {
                script {
                    env.FULL_IMAGE = "${DOCKER_IMAGE_NAME}:${IMAGE_TAG}"
                    env.FULL_IMAGE_LATEST = "${DOCKER_IMAGE_NAME}:latest"
                }
                withCredentials([
                    usernamePassword(
                        credentialsId: "${DOCKER_CREDENTIALS_ID}",
                        usernameVariable: 'DOCKER_USERNAME',
                        passwordVariable: 'DOCKER_PASSWORD'
                    )
                ]) {
                    sh '''
                        chmod +x scripts/docker_publish.sh
                        DOCKER_REGISTRY_HOST="$DOCKER_REGISTRY_HOST" \
                        DOCKER_USERNAME="$DOCKER_USERNAME" \
                        DOCKER_PASSWORD="$DOCKER_PASSWORD" \
                        FULL_IMAGE="$FULL_IMAGE" \
                        FULL_IMAGE_LATEST="$FULL_IMAGE_LATEST" \
                        ./scripts/docker_publish.sh
                    '''
                }
            }
        }

        stage('Deploy') {
            steps {
                withCredentials([
                    usernamePassword(
                        credentialsId: "${DOCKER_CREDENTIALS_ID}",
                        usernameVariable: 'DOCKER_USERNAME',
                        passwordVariable: 'DOCKER_PASSWORD'
                    )
                ]) {
                    sh '''
                        chmod +x scripts/deploy.sh
                        DOCKER_REGISTRY_HOST="$DOCKER_REGISTRY_HOST" \
                        DOCKER_USERNAME="$DOCKER_USERNAME" \
                        DOCKER_PASSWORD="$DOCKER_PASSWORD" \
                        IMAGE_NAME="$FULL_IMAGE_LATEST" \
                        COMPOSE_FILE="$COMPOSE_FILE" \
                        ./scripts/deploy.sh
                    '''
                }
            }
        }
    }

    post {
        success {
            script {
                sendBuildEmail('SUCCESS')
            }
        }
        failure {
            script {
                sendBuildEmail('FAILURE')
            }
        }
        always {
            sh '''
                docker logout "$DOCKER_REGISTRY_HOST" 2>/dev/null || true
                docker image prune -f --filter "dangling=true" || true
            '''
        }
        cleanup {
            cleanWs()
        }
    }
}
