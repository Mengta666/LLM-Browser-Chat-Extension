"""PaddleX 3.7.2 实例级响应适配：保留上游结束原因，不修改全局类。"""


class VLMResultTracker:
    def __init__(self, predictor, versions):
        if versions.get('paddlex') != '3.7.2' or not callable(getattr(predictor, '_doc_vlm_genai_collect_responses', None)):
            raise RuntimeError('unsupported_vlm_response_adapter')
        self.results = []
        predictor._doc_vlm_genai_collect_responses = self.collect

    def reset(self):
        self.results.clear()

    def collect(self, futures):
        contents = []
        for future in futures:
            response = future.result()
            choice = response.choices[0]
            reason = getattr(choice, 'finish_reason', None)
            usage = getattr(response, 'usage', None)
            self.results.append({'finish_reason': reason,
                                 'completion_tokens': getattr(usage, 'completion_tokens', None)})
            contents.append(choice.message.content)
        return contents

    def summary(self):
        reasons = [r['finish_reason'] for r in self.results]
        truncated = True if 'length' in reasons else False if reasons and all(r == 'stop' for r in reasons) else None
        return {'truncated': truncated, 'regions': list(self.results), 'scope': 'vlm_generation_only'}
