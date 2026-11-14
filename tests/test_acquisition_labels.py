from raglex.citations.acquisition_labels import reference_names


def test_inferred_name_is_only_a_label_never_a_matching_or_alias_operation():
 text='Nextia Properties v Royal Bank of Scotland [2013] EWHC 3167'
 class Cat:
  def reference_occurrences(self,*a,**kw):return [{'src_id':'held','context_start':text.index('[2013]')}]
  def get_document(self,sid):return {'payload_hash':'heldhash'}
 class Text:
  def get(self,key):return text
 assert reference_names(Cat(),Text(),'ewhc/ch/2013/3167')==[
  {'name':'Nextia Properties v Royal Bank of Scotland','citing_document':'held','inferred':True}]
