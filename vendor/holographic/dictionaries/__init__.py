"""Language data the matcher reads, each built by a script in tools/ and licensed as its source (texts in LICENSES/):

- cjk_fold.py — Traditional -> Simplified Chinese characters, from OpenCC (Apache-2.0); tools: none (generated once).
- ru_derivation.tsv.gz — Russian words of one root, from DerivBase.Ru in UDer 1.1 (Apache-2.0); build_ru_derivation.py.
- en_derivation.tsv.gz — English words of one root, from Open English Wordnet 2025 (CC BY 4.0, on Princeton WordNet
  3.1); build_en_derivation.py.
- ja_variants.tsv.gz — Japanese spelling variants, from SudachiDict's synonym source (Apache-2.0); build_ja_variants.py.
- zh_words.txt.gz — Chinese words and their frequencies, from jieba's dictionary (MIT); build_zh_words.py.
- de_compounds.tsv.gz, de_es_forms.tsv.gz — German compounds, German and Spanish verb forms, from Wikidata Lexemes
  (CC0); build_de_es_lexicon.py.

Each table loads on the first word of its language; a copy without a table works without that language's extra."""
