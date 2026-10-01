// Job DSL: one pipeline job per jobs/<name>.groovy, run by configuration as
// code at start-up and on every reload.
//
// A job file starts with comment lines:
//   // What the job does, in a sentence or two (becomes the description).
//   // param NAME=default  Help text  (true/false defaults make a checkbox)
//   // cron H */2 * * *               (optional schedule)
// then a declarative pipeline.
//
// Every job can be started from the LAN with a webhook, parameters in the
// query string:
//   curl -X POST 'http://jenkins.local/generic-webhook-trigger/invoke?token=<name>-<secret>&NAME=value'
// where <secret> is ~/.config/brick-jenkins/secrets/webhook_secret on brick9000.

def root = '/brick-cicd-config/brick9000/jenkins'
def secret = new File('/run/secrets/webhook_secret').text.trim()

new File("${root}/jobs").listFiles().findAll { it.name.endsWith('.groovy') }.sort { it.name }.each { file ->
    def name = file.name - '.groovy'
    def text = file.text
    def header = text.readLines().takeWhile { it.startsWith('//') }.collect { it.substring(2).trim() }
    def params = header.findAll { it.startsWith('param ') }.collect { line ->
        def m = line =~ /^param (\w+)=(\S*)\s*(.*)$/
        assert m.matches(): "${file.name}: bad param line: ${line}"
        [name: m.group(1), value: m.group(2), help: m.group(3)]
    }
    def schedule = header.find { it.startsWith('cron ') }?.substring(5)
    def about = header.findAll { !it.startsWith('param ') && !it.startsWith('cron ') }.join('\n')

    pipelineJob(name) {
        description("${about}\n\nWebhook: POST http://jenkins.local/generic-webhook-trigger/invoke?token=${name}-<webhook secret>" +
                    (params ? params.collect { "&${it.name}=…" }.join('') : ''))
        logRotator { numToKeep(30) }
        if (params) {
            parameters {
                params.each { p ->
                    if (p.value in ['true', 'false']) {
                        booleanParam(p.name, p.value == 'true', p.help)
                    } else {
                        stringParam(p.name, p.value, p.help)
                    }
                }
            }
        }
        properties {
            pipelineTriggers {
                triggers {
                    genericTrigger {
                        // Query-string values fill the job's parameters of the same name.
                        genericRequestVariables {
                            params.each { p ->
                                genericRequestVariable {
                                    key(p.name)
                                    regexpFilter('')
                                }
                            }
                        }
                        token("${name}-${secret}")
                        tokenCredentialId('')
                        causeString('Started by webhook')
                        printContributedVariables(false)
                        printPostContent(false)
                        silentResponse(false)
                        shouldNotFlatten(false)
                        regexpFilterText('')
                        regexpFilterExpression('')
                    }
                    if (schedule) {
                        cron {
                            spec(schedule)
                        }
                    }
                }
            }
        }
        definition {
            cps {
                script(text)
                sandbox(true)
            }
        }
    }
}
