import json

import numpy
import pytest
import torch

from allennlp.models import Model
from allennlp.common import Params
from allennlp.commands.train import train_model_from_file
from allennlp.common.testing import ModelTestCase, requires_gpu
from allennlp.nn.beam_search import BeamSearch
from allennlp.nn.util import sequence_cross_entropy_with_logits

from tests import FIXTURES_ROOT


class SimpleSeq2SeqTest(ModelTestCase):
    def setup_method(self):
        super().setup_method()
        self.set_up_model(
            FIXTURES_ROOT / "generation" / "simple" / "experiment.json",
            FIXTURES_ROOT / "generation" / "seq2seq_copy.tsv",
        )

    def test_backwards_compatibility_with_beam_search_args(self):
        # These values are arbitrary but should be different than the config.
        beam_size, max_decoding_steps = 100, 1000
        params = Params.from_file(self.param_file)
        params["model"]["beam_size"] = beam_size
        params["model"]["max_decoding_steps"] = max_decoding_steps
        # The test harness is set up to treat DeprecationWarning's like errors, so this needs to
        # be called within the pytest context manager.
        with pytest.raises(DeprecationWarning):
            model = Model.from_params(vocab=self.vocab, params=params.get("model"))
            assert model._beam_search.beam_size == beam_size
            assert model._beam_search.max_steps == max_decoding_steps

    def test_model_can_train_save_and_load(self):
        self.ensure_model_can_train_save_and_load(self.param_file, tolerance=1e-2)

    @requires_gpu
    def test_model_can_train_with_amp(self):
        train_model_from_file(
            self.param_file,
            self.TEST_DIR,
            overrides="{'trainer.use_amp':true,'trainer.cuda_device':0}",
        )

    def test_bidirectional_model_can_train_save_and_load(self):
        param_overrides = json.dumps({"model.encoder.bidirectional": True})
        self.ensure_model_can_train_save_and_load(
            self.param_file, tolerance=1e-2, overrides=param_overrides
        )

    def test_multi_layer_decoder_model_can_train_save_and_load(self):
        param_overrides = json.dumps({"model.target_decoder_layers": 2})
        self.ensure_model_can_train_save_and_load(
            self.param_file, tolerance=1e-2, overrides=param_overrides
        )

    def test_no_attention_model_can_train_save_and_load(self):
        param_overrides = json.dumps({"model.attention": None})
        self.ensure_model_can_train_save_and_load(
            self.param_file, tolerance=1e-2, overrides=param_overrides
        )

    def test_greedy_model_can_train_save_and_load(self):
        param_overrides = json.dumps({"model.beam_search.beam_size": 1})
        self.ensure_model_can_train_save_and_load(
            self.param_file, tolerance=1e-2, overrides=param_overrides
        )

    def test_loss_is_computed_correctly(self):
        batch_size = 5
        num_decoding_steps = 5
        num_classes = 10
        sample_logits = torch.randn(batch_size, num_decoding_steps - 1, num_classes)
        sample_targets = torch.from_numpy(
            numpy.random.randint(0, num_classes, (batch_size, num_decoding_steps))
        )
        # Mask should be either 0 or 1
        sample_mask = torch.from_numpy(
            numpy.random.randint(0, 2, (batch_size, num_decoding_steps))
        ).bool()
        expected_loss = sequence_cross_entropy_with_logits(
            sample_logits, sample_targets[:, 1:].contiguous(), sample_mask[:, 1:].contiguous()
        )
        actual_loss = self.model._get_loss(sample_logits, sample_targets, sample_mask)
        assert numpy.equal(expected_loss.data.numpy(), actual_loss.data.numpy())

    def test_decode_runs_correctly(self):
        self.model.eval()
        training_tensors = self.dataset.as_tensor_dict()
        output_dict = self.model(**training_tensors)
        decode_output_dict = self.model.make_output_human_readable(output_dict)
        # `make_output_human_readable` should have added a `predicted_tokens` field to
        # `output_dict`. Checking if it's there.
        assert "predicted_tokens" in decode_output_dict

        # The output of model.make_output_human_readable should still have 'predicted_tokens' after
        # using the beam search. To force the beam search, we just remove `target_tokens` from the
        # input tensors.
        del training_tensors["target_tokens"]
        output_dict = self.model(**training_tensors)
        decode_output_dict = self.model.make_output_human_readable(output_dict)
        assert "predicted_tokens" in decode_output_dict

    def test_greedy_decode_matches_beam_search(self):
        beam_search = BeamSearch(
            self.model._end_index, max_steps=self.model._beam_search.max_steps, beam_size=1
        )
        training_tensors = self.dataset.as_tensor_dict()

        # Get greedy predictions from _forward_loop method of model.
        state = self.model._encode(training_tensors["source_tokens"])
        state = self.model._init_decoder_state(state)
        output_dict_greedy = self.model._forward_loop(state)
        output_dict_greedy = self.model.make_output_human_readable(output_dict_greedy)

        # Get greedy predictions from beam search (beam size = 1).
        state = self.model._encode(training_tensors["source_tokens"])
        state = self.model._init_decoder_state(state)
        batch_size = state["source_mask"].size()[0]
        start_predictions = state["source_mask"].new_full(
            (batch_size,), fill_value=self.model._start_index, dtype=torch.long
        )
        all_top_k_predictions, _ = beam_search.search(
            start_predictions, state, self.model.take_step
        )
        output_dict_beam_search = {"predictions": all_top_k_predictions}
        output_dict_beam_search = self.model.make_output_human_readable(output_dict_beam_search)

        # Predictions from model._forward_loop and beam_search should match.
        assert output_dict_greedy["predicted_tokens"] == output_dict_beam_search["predicted_tokens"]
